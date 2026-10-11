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
// the same controls also sit in the row under the prompt, after the status:
// on/off for this session, restart, and Settings, which opens the /config
// options in a pane (or, where no pane can be placed, a box above the
// prompt). They act the moment they are pressed.
//
// The row (or, on a surface without one, the status line) and the settings
// read the proxy's /health for this session.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register } from 'claude-code'

import type { RollingContextHealth as Health, RollingContextSettings as Settings } from '../types'

const PLUGIN = 'rolling-context'
const ACTIONS = ['status', 'on', 'off', 'restart', 'set'] as const
type Action = (typeof ACTIONS)[number]
const LEGACY = ['status', 'on', 'off', 'restart'].map(a => `${PLUGIN}:${a}`)
const STATUS_EVERY_MS = 30_000
const USAGE =
  'Usage: /roll [status|on|off|restart] [--global], or /roll set <trigger|target|retention> <value>'
const PANE = 'settings'


// The /config options the settings and /roll set change, by the names /roll set takes.
const FIELDS = {
  trigger: 'trigger_tokens',
  target: 'target_tokens',
  retention: 'retention',
} as const
type Field = keyof typeof FIELDS


// What the row and the settings draw from: set by refreshStatus, the settings
// loader and the controls. isOpen is the fallback box above the prompt, shown
// only where the settings pane could not be placed.
const healthNow = atom({ plugin: 'rolling-context', key: 'health' } as const, null)
const isOpen = atom({ plugin: 'rolling-context', key: 'isOpen' } as const, false)
const settingsNow = atom({ plugin: 'rolling-context', key: 'settings' } as const, null)
const busyNow = atom({ plugin: 'rolling-context', key: 'busy' } as const, null)

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
// Whether a surface draws the row under the prompt: the status is there
// then, and the engine's status line would only repeat it.
let rowDrawn = false

async function refreshStatus($: EngineInterface) {
  const h = await health($, await $.session.id())
  await update($, healthNow, () => h ?? null)
  $.ui.status(showStatus && !rowDrawn ? statusText(h) : undefined)
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

async function restart($: EngineInterface): Promise<string> {
  const root = $.plugin.root
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
    return `Restart failed to run: ${String(err)}`
  }
  try {
    const log = await $.fs.read(`${await home($)}/.claude/rolling-context-hook.log`)
    return log.trimEnd().split(/\r?\n/).slice(-6).join('\n')
  } catch {
    return '(no hook log found)'
  }
}

async function run($: EngineInterface, args: string): Promise<string> {
  const words = args.trim().split(/\s+/).filter(Boolean)
  const verb = words.findIndex(w => !w.startsWith('-'))
  const action = (verb < 0 ? 'status' : words[verb]!).toLowerCase()
  const flags = words.filter(w => w.startsWith('-'))
  if (!(ACTIONS as readonly string[]).includes(action)) {
    return `Unknown action "${action}". ${USAGE}`
  }

  if ((action as Action) === 'set') {
    const rest = words.slice(verb + 1)
    const field = (rest[0] ?? '').toLowerCase()
    if (!(field in FIELDS) || rest.length < 2) return USAGE
    return setOption($, field as Field, rest.slice(1).join(' '))
  }

  if ((action as Action) === 'restart') {
    await update($, busyNow, () => 'restarting the proxy…')
    if (!rowDrawn && showStatus) $.ui.status('restarting the proxy…')
    try {
      return await restart($)
    } finally {
      await update($, busyNow, () => null)
      await refreshStatus($)
    }
  }

  const r = await runPython(
    $,
    [`${$.plugin.root}/proxy/switch.py`, action, ...flags, '--session', await $.session.id()],
    { ROLLING_CONTEXT_VIA: 'mod', PYTHONIOENCODING: 'utf-8' },
  )
  await refreshStatus($)
  const out = clean(r.stdout) || clean(r.stderr)
  return r.exitCode === 0 ? out : `switch.py exited ${r.exitCode}:\n${out}`
}

// A control: do it, and say how it went in a toast.
async function act($: EngineInterface, work: Promise<string>) {
  try {
    const out = await work
    $.ui.toast(out.split('\n')[0] ?? out)
  } catch (err) {
    $.ui.toast(`rolling-context: ${String(err)}`)
  }
}

async function paneOpen($: EngineInterface) {
  return (await $.ui.panes()).some(p => p.id === PANE)
}

// Settings: a pane of their own, closed again by the same button. A press is
// the person asking, so the surface places it at any width; where it still is
// not placed, the box above the prompt shows them instead.
async function toggleSettings($: EngineInterface) {
  if (await paneOpen($)) return $.ui.close({ id: PANE })
  if (await read($, isOpen)) return update($, isOpen, () => false)
  await loadSettings($)
  const opened = await $.ui.open({
    id: PANE, title: 'Rolling context settings', focus: true, closeOnEscape: true, rows: 5,
  })
  if (!opened.isPlaced) {
    await $.ui.close({ id: PANE })
    await update($, isOpen, () => true)
  }
}

// The settings, in the pane or the fallback box: the two token counts as
// number fields (Enter sets one; 150k works too) and the retention choice.
function settingsBody(
  $: EngineInterface,
  { Box, Input, Select, Text }: Elements['terminal'],
  settings: Settings,
) {
  return (
    <Box flexDirection="column">
      <Input
        key="trigger"
        label="Compress above (tokens) "
        value={String(settings.trigger)}
        placeholder="200000"
        onSubmit={v => act($, setOption($, 'trigger', v))}
      />
      <Input
        key="target"
        label="Keep verbatim (tokens) "
        value={String(settings.target)}
        placeholder="100000"
        onSubmit={v => act($, setOption($, 'target', v))}
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
      <Text dimColor>Enter sets a number; the proxy reads it on its next request.</Text>
    </Box>
  )
}

// A command hook that throws would otherwise fall through to the .md
// command (a Claude turn) or to "no hook answered": say what went wrong.
function failed(_$: unknown, _e: unknown, next: { error?: { message?: string } }) {
  return { text: `rolling-context: ${next.error?.message ?? 'the command failed'}` }
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

  // The row under the prompt: the status, bold in the theme's text color,
  // then the controls; the engine's own hint (`? for shortcuts`) follows,
  // dim, so nothing of it is lost.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    if (!showStatus && !showControls) return next(e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    if (!rowDrawn) {
      rowDrawn = true
      try {
        $.ui.status(undefined)
      } catch {
        // the next refreshStatus clears it
      }
    }
    const now = await read($, healthNow) // not `h`: that is the JSX factory
    const busy = await read($, busyNow)
    const shown = (await read($, isOpen)) || (await paneOpen($))
    const { Box, Button, Text } = $.ui.resolve(e)

    const toggle = !now
      ? { label: 'Start proxy', args: 'restart' }
      : isOn(now)
        ? { label: 'Turn off', args: 'off' }
        : now.session?.scope === 'machine-wide'
          ? { label: 'Turn on everywhere', args: 'on --global' }
          : { label: 'Turn on', args: 'on' }

    return (
      <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
        {showStatus ? (
          <Text bold color="text">{`Rolling context: ${busy ?? statusText(now)}`}</Text>
        ) : null}
        {showControls ? (
          <Box flexDirection="row" columnGap={1}>
            <Button key="toggle" label={toggle.label} onPress={() => act($, run($, toggle.args))} />
            <Button
              key="settings"
              label={shown ? 'Hide settings' : 'Settings'}
              onPress={() => toggleSettings($)}
            />
            {now ? (
              <Button key="restart" label="Restart" onPress={() => act($, run($, 'restart'))} />
            ) : null}
          </Box>
        ) : null}
        {e.props.hint ? <Text dimColor>{e.props.hint}</Text> : null}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    // Loaded by the Settings button before it opens the pane; a draw never writes.
    const settings = await read($, settingsNow)
    const els = $.ui.resolve(e) as Elements['terminal']
    if (!settings) return <els.Text dimColor>Loading the settings…</els.Text>
    return settingsBody($, els, settings)
  })

  // The fallback: the same settings in the box above the prompt, toggled by
  // the Settings button, where the pane could not be placed.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, isOpen))) return next(e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    const settings = await read($, settingsNow)
    if (!settings) return next(e)
    return settingsBody($, $.ui.resolve(e) as Elements['terminal'], settings)
  })
}
