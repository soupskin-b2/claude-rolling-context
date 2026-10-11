// rolling-context's mod: the switch and status, without a Claude turn.
//
// The plugin's commands/*.md are prompt templates: Claude has to read one,
// run switch.py through Bash and echo the result, and while it is mid-turn
// they wait for the turn to end. Here /rc runs switch.py itself, at once,
// even mid-turn (`immediate`), and prints its output as the command's own.
// The .md commands stay as the fallback where mods do not load; where they
// do, this module answers them too (still no Claude turn) and hides them
// from the menu so /rc is the one listed.
//
// The status line polls the proxy's /health for this session's state.
import type { EngineInterface, Register } from 'claude-code'

const ACTIONS = ['status', 'on', 'off', 'restart'] as const
type Action = (typeof ACTIONS)[number]
const LEGACY = ACTIONS.map(a => `rolling-context:${a}`)
const STATUS_EVERY_MS = 30_000

type Health = {
  service?: string
  version?: string
  enabled?: boolean
  compression_count?: number
  total_tokens_saved?: number
  stored_compressions?: number
  active_compressions?: number
  session?: { enabled: boolean; scope: string }
}

const compact = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n)

async function isWindows($: EngineInterface) {
  return (await $.env.get('OS')) === 'Windows_NT'
}

async function home($: EngineInterface) {
  return (await isWindows($))
    ? ((await $.env.get('USERPROFILE')) ?? '')
    : ((await $.env.get('HOME')) ?? '')
}

// `python` on Windows (where `python3` is often the Store stub), otherwise
// `python3` with `python` as the fallback, as start-proxy.sh picks.
async function runPython($: EngineInterface, args: string[], env: Record<string, string>) {
  const candidates = (await isWindows($)) ? ['python'] : ['python3', 'python']
  let lastError: unknown
  for (const exe of candidates) {
    try {
      return await $.process.run([exe, ...args], { env, timeoutMs: 15_000 })
    } catch (err) {
      lastError = err
    }
  }
  throw lastError
}

async function health($: EngineInterface, sessionId: string): Promise<Health | undefined> {
  const port = (await $.env.get('ROLLING_CONTEXT_PORT')) || '5588'
  try {
    const r = await $.http.fetch(
      `http://127.0.0.1:${port}/health?session=${encodeURIComponent(sessionId)}`,
    )
    if (!r.ok) return undefined
    const body = JSON.parse(r.text) as Health
    return body.service === 'rolling-context' ? body : undefined
  } catch {
    return undefined
  }
}

function statusText(h: Health | undefined) {
  if (!h) return 'proxy not running (it starts with the next session, or /rc restart)'
  const s = h.session
  const on = s ? s.enabled : h.enabled !== false
  const state = on
    ? 'on'
    : s?.scope === 'machine-wide'
      ? 'off machine-wide'
      : 'off for this session'
  const busy = h.active_compressions ? ' · compressing…' : ''
  return `${state} · ${compact(h.total_tokens_saved ?? 0)} tokens saved · ` +
    `${h.stored_compressions ?? 0} stored${busy}`
}

// Drop the legacy marker lines: machinery, not output.
const clean = (text: string) =>
  text
    .split(/\r?\n/)
    .filter(line => !line.includes('<<rolling-context:'))
    .join('\n')
    .trim()

// Set from the options in register; /config changes reload the module.
let showStatus = true

async function refreshStatus($: EngineInterface) {
  if (!showStatus) return
  $.ui.status(statusText(await health($, await $.session.id())))
}

async function run($: EngineInterface, args: string): Promise<string> {
  const words = args.trim().split(/\s+/).filter(Boolean)
  const action = (words.find(w => !w.startsWith('-')) ?? 'status').toLowerCase()
  const flags = words.filter(w => w.startsWith('-'))
  if (!(ACTIONS as readonly string[]).includes(action)) {
    return `Unknown action "${action}". Usage: /rc [status|on|off|restart] [--global]`
  }
  const sessionId = await $.session.id()
  const root = $.plugin.root

  if ((action as Action) === 'restart') {
    $.ui.status('restarting the proxy…')
    const env = { ROLLING_CONTEXT_FORCE_RESTART: '1' }
    try {
      if (await isWindows($)) {
        await $.process.run(
          ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            `${root}/hooks/start-proxy.ps1`],
          { env, timeoutMs: 60_000 },
        )
      } else {
        await $.process.run(['bash', `${root}/hooks/start-proxy.sh`], { env, timeoutMs: 60_000 })
      }
    } catch (err) {
      await refreshStatus($)
      return `Restart failed to run: ${String(err)}`
    }
    let tail = ''
    try {
      const log = await $.fs.read(`${await home($)}/.claude/rolling-context-hook.log`)
      tail = log.trimEnd().split(/\r?\n/).slice(-6).join('\n')
    } catch {
      tail = '(no hook log found)'
    }
    await refreshStatus($)
    return tail
  }

  const r = await runPython(
    $,
    [`${root}/proxy/switch.py`, action, ...flags, '--session', sessionId],
    { ROLLING_CONTEXT_VIA: 'mod', PYTHONIOENCODING: 'utf-8' },
  )
  await refreshStatus($)
  const out = clean(r.stdout) || clean(r.stderr)
  return r.exitCode === 0 ? out : `switch.py exited ${r.exitCode}:\n${out}`
}

// A command hook that throws would otherwise fall through to the .md
// command (a Claude turn) or to "no hook answered": say what went wrong.
function failed(_$: unknown, _e: unknown, next: { error?: { message?: string } }) {
  return { text: `rolling-context: ${next.error?.message ?? 'the command failed'}` }
}

export const register: Register = (on, options) => {
  showStatus = options.status_line !== false

  on('session.start', async ($, e, next) => {
    if (showStatus) {
      void refreshStatus($)
      $.clock.every(STATUS_EVERY_MS, () => void refreshStatus($))
    } else {
      $.ui.status(undefined)
    }
    // Last: a refused name throws, and nothing after it would run.
    await $.command.register({
      name: 'rc',
      description: 'rolling-context: status, or turn it on/off for this session (--global for all), or restart the proxy',
      argumentHint: '[status|on|off|restart] [--global]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'rc' }, async ($, e) => ({ text: await run($, e.args) }))
    .catch(failed)

  // The plugin's own /rolling-context:<action> commands: answered here, so
  // no Claude turn, and kept out of the menu in favour of /rc.
  for (const name of LEGACY) {
    const action = name.split(':')[1]
    on('command.run', { command: name }, async ($, e) => ({
      text: await run($, `${action} ${e.args}`),
    })).catch(failed)
    on('command.describe', { command: name }, async ($, e, next) => ({
      ...(await next(e)),
      isHidden: true,
    }))
  }

  // A turn may have triggered a compression; show it without waiting 30 s.
  on('turn.complete', async ($, e, next) => {
    void refreshStatus($)
    return next(e)
  })
}
