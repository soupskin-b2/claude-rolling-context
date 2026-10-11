// rolling-context's mod: the switch, the settings and the status, without a
// Claude turn.
//
// The plugin's commands/*.md are prompt templates: Claude has to read one,
// run switch.py through Bash and echo the result, and while it is mid-turn
// they wait for the turn to end. Here /roll runs switch.py itself and prints
// its output as the command's own. The .md commands stay as the fallback
// where mods do not load; where they do, this module answers them too (still
// no Claude turn) and hides them from the menu so /roll is the one listed.
//
// A command typed mid-turn may still wait for the turn on some surfaces, so
// the same controls also sit in a band above the prompt: on/off for this
// session and the /config options, which act the moment they are pressed.
//
// The status line and the band read the proxy's /health for this session.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RollingContextHealth as Health, RollingContextSettings as Settings } from '../types'

const PLUGIN = 'rolling-context'
const ACTIONS = ['status', 'on', 'off', 'restart', 'set'] as const
type Action = (typeof ACTIONS)[number]
const LEGACY = ['status', 'on', 'off', 'restart'].map(a => `${PLUGIN}:${a}`)
const STATUS_EVERY_MS = 30_000
const USAGE =
  'Usage: /roll [status|on|off|restart] [--global], or /roll set <trigger|target|retention> <value>'


// The /config options the band and /roll set change, by the names /roll set takes.
const FIELDS = {
  trigger: 'trigger_tokens',
  target: 'target_tokens',
  retention: 'retention',
} as const
type Field = keyof typeof FIELDS


// What the band draws from: set by refreshStatus and the settings loader.
const healthNow = atom({ plugin: 'rolling-context', key: 'health' } as const, null)
const isOpen = atom({ plugin: 'rolling-context', key: 'isOpen' } as const, false)
const settingsNow = atom({ plugin: 'rolling-context', key: 'settings' } as const, null)

const TRIGGER_PRESETS = [50_000, 100_000, 150_000, 200_000, 300_000, 500_000, 800_000]
const TARGET_PRESETS = [20_000, 40_000, 60_000, 80_000, 100_000, 150_000, 200_000]

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

// $.http.fetch takes no timeout, and a fetch that never settles (the engine
// sends it through HTTPS_PROXY, to a proxy that may be mid-restart or too old
// to relay it) held every /roll command open until the user stopped it.
const HEALTH_TIMEOUT_MS = 2_000

async function health($: EngineInterface, sessionId: string): Promise<Health | undefined> {
  const port = (await $.env.get('ROLLING_CONTEXT_PORT')) || '5588'
  try {
    const r = await Promise.race([
      $.http.fetch(`http://127.0.0.1:${port}/health?session=${encodeURIComponent(sessionId)}`),
      $.clock.sleep(HEALTH_TIMEOUT_MS).then(() => undefined),
    ])
    if (!r || !r.ok) return undefined
    const body = JSON.parse(r.text) as Health
    return body.service === 'rolling-context' ? body : undefined
  } catch {
    return undefined
  }
}

function isOn(h: Health) {
  return h.session ? h.session.enabled : h.enabled !== false
}

function statusText(h: Health | null | undefined) {
  if (!h) return 'proxy not running (it starts with the next session, or /roll restart)'
  const state = isOn(h)
    ? 'on'
    : h.session?.scope === 'machine-wide'
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
let showControls = true
// Whether /roll was registered: the .md commands are hidden only then.
let rollRegistered = false

async function refreshStatus($: EngineInterface) {
  const h = await health($, await $.session.id())
  await update($, healthNow, () => h ?? null)
  if (showStatus) $.ui.status(statusText(h))
}

// This plugin's /config rows.
async function optionRows($: EngineInterface) {
  const rows = await $.config.list()
  return rows.filter(row => row.provider.plugin === PLUGIN)
}

async function loadSettings($: EngineInterface) {
  const rows = await optionRows($)
  const row = (field: string) => rows.find(r => r.key.endsWith(`.${field}`))
  const retention = row(FIELDS.retention)
  const settings: Settings = {
    trigger: Number(row(FIELDS.trigger)?.value ?? 100_000),
    target: Number(row(FIELDS.target)?.value ?? 40_000),
    retention: String(retention?.value ?? '14 days'),
    retentionOptions: [...(retention?.options ?? [])],
  }
  await update($, settingsNow, () => settings)
  return settings
}

// "200000", "200k", "1.5m" as a token count; NaN for anything else.
function parseTokens(text: string) {
  const m = /^(\d+(?:\.\d+)?)\s*([km]?)$/i.exec(text.trim().replace(/[,_]/g, ''))
  if (!m) return NaN
  const scale = m[2]?.toLowerCase() === 'm' ? 1e6 : m[2]?.toLowerCase() === 'k' ? 1e3 : 1
  return Math.round(Number(m[1]) * scale)
}

async function setOption($: EngineInterface, field: Field, raw: string): Promise<string> {
  const rows = await optionRows($)
  const row = rows.find(r => r.key.endsWith(`.${FIELDS[field]}`))
  if (!row) return `No /config row for ${field}: is the plugin enabled?`
  let value: string | number
  if (field === 'retention') {
    const want = raw.trim().toLowerCase().replace(/\s+/g, ' ')
    const match = (row.options ?? []).find(o => o.toLowerCase() === want)
    if (!match) return `retention takes one of: ${(row.options ?? []).join(', ')}`
    value = match
  } else {
    value = parseTokens(raw)
    if (!Number.isFinite(value)) return `${field} takes a token count, such as 150000 or 150k`
  }
  const { deny } = await $.config.set({ key: row.key, value })
  if (deny) return `Not changed: ${deny}`
  await loadSettings($)
  const shown = typeof value === 'number' ? `${value.toLocaleString('en-US')} tokens` : value
  return `${field} set to ${shown}. The proxy reads it on its next request.`
}

async function run($: EngineInterface, args: string): Promise<string> {
  const words = args.trim().split(/\s+/).filter(Boolean)
  const verb = words.findIndex(w => !w.startsWith('-'))
  const action = (verb < 0 ? 'status' : words[verb]!).toLowerCase()
  const flags = words.filter(w => w.startsWith('-'))
  if (!(ACTIONS as readonly string[]).includes(action)) {
    return `Unknown action "${action}". ${USAGE}`
  }
  const root = $.plugin.root

  if ((action as Action) === 'set') {
    const rest = words.slice(verb + 1)
    const field = (rest[0] ?? '').toLowerCase()
    if (!(field in FIELDS) || rest.length < 2) return USAGE
    return setOption($, field as Field, rest.slice(1).join(' '))
  }

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
    [`${root}/proxy/switch.py`, action, ...flags, '--session', await $.session.id()],
    { ROLLING_CONTEXT_VIA: 'mod', PYTHONIOENCODING: 'utf-8' },
  )
  await refreshStatus($)
  const out = clean(r.stdout) || clean(r.stderr)
  return r.exitCode === 0 ? out : `switch.py exited ${r.exitCode}:\n${out}`
}

// A band control: do it, and say how it went in a toast.
async function act($: EngineInterface, work: Promise<string>) {
  try {
    const out = await work
    $.ui.toast(out.split('\n')[0] ?? out)
  } catch (err) {
    $.ui.toast(`rolling-context: ${String(err)}`)
  }
}

async function toggleSettings($: EngineInterface) {
  if (!(await read($, isOpen))) await loadSettings($)
  await update($, isOpen, v => !v)
}

// A command hook that throws would otherwise fall through to the .md
// command (a Claude turn) or to "no hook answered": say what went wrong.
function failed(_$: unknown, _e: unknown, next: { error?: { message?: string } }) {
  return { text: `rolling-context: ${next.error?.message ?? 'the command failed'}` }
}

// The presets, with the current value among them so the Select can show it.
function choices(presets: number[], current: number) {
  const all = presets.includes(current) ? presets : [...presets, current].sort((a, b) => a - b)
  return all.map(n => ({ value: String(n), label: `${compact(n)} tokens` }))
}

export const register: Register = (on, options) => {
  showStatus = options.status_line !== false
  showControls = options.controls !== false

  on('session.start', async ($, e, next) => {
    void refreshStatus($)
    $.clock.every(STATUS_EVERY_MS, () => void refreshStatus($))
    if (!showStatus) $.ui.status(undefined)
    // A name another command already holds (a built-in or its alias, as /rc
    // is /remote-control's) is refused with a throw. Then the plugin's own
    // /rolling-context:* commands stay listed, and say so, rather than leave
    // nothing in the menu.
    try {
      await $.command.register({
        name: 'roll',
        description: 'rolling-context: status, on/off for this session (--global for all), set an option, or restart the proxy',
        argumentHint: '[status|on|off|restart|set <option> <value>] [--global]',
        immediate: true,
      })
      rollRegistered = true
    } catch (err) {
      rollRegistered = false
      $.ui.log(`/roll is unavailable (${String(err)}); use /rolling-context:status, :on, :off and :restart instead`)
    }
    // The menu caches each command's description for the session, and may
    // have asked before /roll existed: ask again, so the .md commands hide.
    $.ui.invalidate('command.describe')
    return next(e)
  })

  on('command.run', { command: 'roll' }, async ($, e) => ({ text: await run($, e.args) }))
    .catch(failed)

  // The plugin's own /rolling-context:<action> commands: answered here, so
  // no Claude turn, and kept out of the menu in favour of /roll.
  for (const name of LEGACY) {
    const action = name.split(':')[1]
    on('command.run', { command: name }, async ($, e) => ({
      text: await run($, `${action} ${e.args}`),
    })).catch(failed)
    on('command.describe', { command: name }, async ($, e, next) =>
      rollRegistered ? { ...(await next(e)), isHidden: true } : next(e))
  }

  // A turn may have triggered a compression; show it without waiting 30 s.
  on('turn.complete', async ($, e, next) => {
    void refreshStatus($)
    return next(e)
  })

  // The band above the prompt: the state, an on/off switch and the settings.
  // Its controls act when pressed, mid-turn included; the engine's [-]
  // folds it away.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!showControls || e.props.hasSurvey) return next(e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    const now = await read($, healthNow) // not `h`: that is the JSX factory
    const open = await read($, isOpen)
    const settings = await read($, settingsNow)
    const { Box, Button, Select, Text } = $.ui.resolve(e)

    const toggle = !now
      ? { label: 'Start proxy', args: 'restart' }
      : isOn(now)
        ? { label: 'Turn off for this session', args: 'off' }
        : now.session?.scope === 'machine-wide'
          ? { label: 'Turn on for all sessions', args: 'on --global' }
          : { label: 'Turn on for this session', args: 'on' }

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
          <Text bold>Rolling context</Text>
          <Text dimColor>{statusText(now)}</Text>
          <Button key="toggle" label={toggle.label} onPress={() => act($, run($, toggle.args))} />
          <Button
            key="settings"
            label={open ? 'Hide settings' : 'Settings'}
            dimColor
            onPress={() => toggleSettings($)}
          />
        </Box>
        {open && settings ? (
          <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
            <Select
              key="trigger"
              label="Compress above "
              options={choices(TRIGGER_PRESETS, settings.trigger)}
              value={String(settings.trigger)}
              onSelect={v => act($, setOption($, 'trigger', v))}
            />
            <Select
              key="target"
              label="Keep verbatim "
              options={choices(TARGET_PRESETS, settings.target)}
              value={String(settings.target)}
              onSelect={v => act($, setOption($, 'target', v))}
            />
            <Select
              key="retention"
              label="Delete unused compressions after "
              options={(settings.retentionOptions.length
                ? settings.retentionOptions
                : [settings.retention]).map(o => ({ value: o }))}
              value={settings.retention}
              onSelect={v => act($, setOption($, 'retention', v))}
            />
            <Button key="restart" label="Restart proxy" dimColor onPress={() => act($, run($, 'restart'))} />
          </Box>
        ) : null}
      </Box>
    )
  })
}
