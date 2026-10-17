// rolling-context's mod: the switch, the settings and the status, without a
// Claude turn.
//
// /roll runs switch.py itself and prints its output as the command's own: no
// prompt template for Claude to read, run through Bash and echo, and nothing
// that waits for a turn to end. It is the plugin's only command.
//
// A command typed mid-turn may still wait for the turn on some surfaces, so
// the same controls also sit beside the status: in the row under the prompt,
// in Toggle/Restart/Settings order, on surfaces that draw one (the terminal),
// and in one compact "RC" line in the band above the prompt where no row
// under the prompt is drawn (the desktop app draws that footer itself). The
// band is the only place on the desktop where buttons can sit — the status
// line takes text only, a pane has to be opened, toasts fade — so it stays to
// that one line. Settings opens the /config options in a pane (or, where no
// pane can be placed, a box above the prompt). The buttons act the moment
// they are pressed.
//
// The status line under the prompt always carries the status, and the row,
// the band and the settings read the proxy's /health for this session.
//
// Sessions, in the band, opens a pane that moves a stopped desktop app session to another
// account signed in on this computer (proxy/move_session.py says how, and why
// the transcript, scratchpad and compressions need nothing moved). Its pane
// also imports: it lists another account's sessions and moves the one picked
// into this session's account.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register } from 'claude-code'

import type {
  RollingContextAccount as Account,
  RollingContextHealth as Health,
  RollingContextImportList as ImportList,
  RollingContextImportSession as ImportSession,
  RollingContextMove as Move,
  RollingContextSettings as Settings,
} from '../types'

const PLUGIN = 'rolling-context'
const ACTIONS = ['status', 'on', 'off', 'restart', 'set'] as const
type Action = (typeof ACTIONS)[number]
const STATUS_EVERY_MS = 30_000
const USAGE =
  'Usage: /roll [status|on|off|restart] [--global], or /roll set <trigger|target|retention> <value>'
const PANE = 'settings'
const MOVE_PANE = 'move'
// The desktop app's session tools, as its MCP server is named.
const APP_SESSIONS = 'ccd_session_mgmt'

// The retention choices from userConfig, when the /config row carries no
// options of its own (then the dropdown would offer only the current value).
const RETENTION_OPTIONS = [
  '1 hour', '6 hours', '1 day', '3 days', '7 days', '14 days', '30 days', 'Never',
]


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
const ctxNow = atom({ plugin: 'rolling-context', key: 'ctx' } as const, null)
// The Sessions pane's: the accounts answer, its fallback box, the pick
// and what a move in progress says.
const moveNow = atom({ plugin: 'rolling-context', key: 'move' } as const, null)
const moveOpen = atom({ plugin: 'rolling-context', key: 'moveOpen' } as const, false)
const moveTo = atom({ plugin: 'rolling-context', key: 'moveTo' } as const, null)
const moveBusy = atom({ plugin: 'rolling-context', key: 'moveBusy' } as const, null)
// Its import section's: the account, its sessions, the pick, whether the
// archived ones show and what an import in progress says.
const importFrom = atom({ plugin: 'rolling-context', key: 'importFrom' } as const, null)
const importList = atom({ plugin: 'rolling-context', key: 'importList' } as const, null)
const importPick = atom({ plugin: 'rolling-context', key: 'importPick' } as const, null)
const importArchived = atom({ plugin: 'rolling-context', key: 'importArchived' } as const, false)
const importBusy = atom({ plugin: 'rolling-context', key: 'importBusy' } as const, null)
// Its Switch account section's: the account picked and what it says while a
// switch starts.
const switchTo = atom({ plugin: 'rolling-context', key: 'switchTo' } as const, null)
const switchBusy = atom({ plugin: 'rolling-context', key: 'switchBusy' } as const, null)

const compact = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n)

// Context tokens with one decimal for thousands: 302500 becomes "302.5k".
const compactCtx = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
  : n >= 1e3 ? `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}k`
  : String(n)

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
    if (!r) return noteHealth($, `no answer from :${port} within ${HEALTH_TIMEOUT_MS} ms`)
    if (!r.ok) return noteHealth($, `:${port} answered HTTP ${r.status}`)
    const body = JSON.parse(r.text) as Health
    if (body.service !== 'rolling-context') return noteHealth($, `:${port} is not this proxy`)
    noteHealth($, `ok: v${body.version ?? '?'} on :${port}`)
    return body
  } catch (err) {
    return noteHealth($, `fetch of :${port} failed: ${String(err)}`)
  }
}

function isOn(h: Health) {
  return h.session ? h.session.enabled : h.enabled !== false
}

// The toggle for this session's state: starting the proxy where none answers.
function toggleAction(now: Health | null) {
  if (!now) return { label: 'Start proxy', args: 'restart' }
  if (isOn(now)) return { label: 'Turn off', args: 'off' }
  return now.session?.scope === 'machine-wide'
    ? { label: 'Turn on everywhere', args: 'on --global' }
    : { label: 'Turn on', args: 'on' }
}

function statusText(h: Health | null | undefined, ctx: number | null) {
  if (!h) return 'proxy not running (it starts with the next session, or /roll restart)'
  const state = isOn(h)
    ? 'on'
    : h.session?.scope === 'machine-wide'
      ? 'off machine-wide'
      : 'off for this session'
  // While a compression runs it takes the state and context's place: tacked
  // on the end, the line ran past the width the host gives it and was cut.
  const head = h.active_compressions
    ? 'compressing…'
    : ctx == null ? state : `${state} · ${compactCtx(ctx)} context`
  // This session's compressions and savings, then what the proxy keeps for
  // every session. A proxy older than 1.14.9 counts no session's share.
  const mine = h.session?.compressions == null ? [] : [
    `${h.session.compressions} compression${h.session.compressions === 1 ? '' : 's'}`,
    `${compact(h.session.tokens_saved ?? 0)} tokens saved`,
  ]
  const counts = [...mine, `${h.stored_compressions ?? 0} stored`].join(', ')
  return `${head} · ${counts}`
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
let showMove = true
// Lookups left for a session the app has no record of yet: the app may write
// a new session's record after the session starts, so it is looked for again
// on each status refresh for the first ten minutes.
let moveTries = 20
// Whether a model turn runs: a session moves only while stopped.
let working = false
// Whether a surface draws the row under the prompt; then the band stays
// empty (its buttons already sit in the row).
let rowDrawn = false

// The session's context tokens, or null when the engine has none to report.
async function contextTokens($: EngineInterface): Promise<number | null> {
  try {
    const tokens = (await $.session.usage())?.context?.tokens
    return typeof tokens === 'number' ? tokens : null
  } catch {
    return null
  }
}

// The status line under the prompt always carries the status: the row and
// the band hold the buttons, and the line holds the words.
async function refreshStatus($: EngineInterface) {
  const sessionId = await $.session.id()
  const [h, ctx] = await Promise.all([health($, sessionId), contextTokens($)])
  await update($, healthNow, () => h ?? null)
  await update($, ctxNow, () => ctx)
  $.ui.status(showStatus ? statusText(h, ctx) : undefined)
}

// This plugin's /config rows. The key is `<plugin>.<field>` (the engine has
// keyed it `rolling-context.<field>` whether the plugin loaded from a
// marketplace or inline); the provider names the plugin id, the bare name,
// `<name>@<marketplace>` or `<name>@inline`. Either one is enough: the
// provider's spelling has already changed under us once.
const ours = (id: string) => id === PLUGIN || id.startsWith(`${PLUGIN}@`)

function isOurs(row: { key: string; provider?: { plugin?: string } }) {
  const dot = row.key.lastIndexOf('.')
  return (dot > 0 && ours(row.key.slice(0, dot))) || ours(row.provider?.plugin ?? '')
}

async function optionRows($: EngineInterface) {
  const rows = (await $.config.list()).filter(isOurs)
  await recordConfigId($, rows[0]?.provider?.plugin)
  return rows
}

// The proxy reads the options from settings.json's pluginConfigs, which can
// hold more than one entry for this plugin: the desktop app's rows belong to
// `rolling-context@inline` while `@<marketplace>` is the enabled id. Tell it
// which entry the rows the user edits are, so a save reaches the proxy.
let recordedId = ''
async function recordConfigId($: EngineInterface, id: string | undefined) {
  if (!id || !ours(id) || id === recordedId) return
  try {
    const path = `${await home($)}/.claude/rolling-context-config-id`
    if ((await $.fs.read(path).catch(() => '')).trim() !== id) await $.fs.write(path, `${id}\n`)
    recordedId = id
  } catch {
    // The proxy falls back to the enabled id.
  }
}

// The options as settings.json holds them, for a session without rows: the
// entry the rows were last recorded under first, as the proxy reads them.
async function storedOptions($: EngineInterface): Promise<Record<string, unknown>> {
  try {
    const dir = `${await home($)}/.claude`
    const configs = JSON.parse(await $.fs.read(`${dir}/settings.json`))?.pluginConfigs ?? {}
    const live = (await $.fs.read(`${dir}/rolling-context-config-id`).catch(() => '')).trim()
    const ids = Object.keys(configs).filter(ours).sort((a, b) => Number(b === live) - Number(a === live))
    return ids.map(i => configs[i]?.options).find(o => o && typeof o === 'object') ?? {}
  } catch {
    return {}
  }
}

async function loadSettings($: EngineInterface) {
  const rows = await optionRows($)
  const stored = rows.length ? {} : await storedOptions($)
  const row = (field: string) =>
    rows.find(r => r.key.endsWith(`.${field}`)) ??
    (stored[field] === undefined ? undefined : { value: stored[field], options: undefined })
  const retention = row(FIELDS.retention)
  const retentionOpts = retention?.options
  const settings: Settings = {
    trigger: Number(row(FIELDS.trigger)?.value ?? 100_000),
    target: Number(row(FIELDS.target)?.value ?? 40_000),
    retention: String(retention?.value ?? '14 days'),
    retentionOptions: [...(retentionOpts?.length ? retentionOpts : RETENTION_OPTIONS)],
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

// No row for the field: say what /config does hold for it and where this copy
// of the plugin loaded from, so a report carries the cause.
async function noRow($: EngineInterface, field: Field) {
  const all = await $.config.list()
  const near = all
    .filter(r => r.key.endsWith(`.${FIELDS[field]}`) || r.key.includes(PLUGIN))
    .map(r => `${r.key} (${r.provider?.plugin ?? '?'})`)
  return `No /config row for ${field}: is the plugin enabled? ` +
    `/config has ${all.length} rows; matching: ${near.join(', ') || 'none'}; ` +
    `plugin loaded from ${$.plugin.root}`
}

// A desktop session sometimes starts with none of the plugin's rows in
// /config: the engine builds them from its list of enabled plugins and has
// left this one out, though the plugin and this mod are loaded. switch.py then
// writes the option into settings.json's pluginConfigs itself, where both the
// proxy and the next session's /config read it.
async function setDirect($: EngineInterface, field: Field, value: string | number) {
  const r = await runPython(
    $,
    [`${$.plugin.root}/proxy/switch.py`, 'set', FIELDS[field], String(value)],
    { PYTHONIOENCODING: 'utf-8' },
  )
  if (r.exitCode !== 0) {
    return `Not changed: ${clean(r.stdout) || clean(r.stderr) || `switch.py exited ${r.exitCode}`}\n` +
      (await noRow($, field))
  }
  await loadSettings($)
  return `${field} set to ${shown(value)} in settings.json (this session's /config ` +
    `has no rows for the plugin; /reload-plugins brings them back). The proxy reads it on its next request.`
}

const shown = (value: string | number) =>
  typeof value === 'number' ? `${value.toLocaleString('en-US')} tokens` : value

async function setOption($: EngineInterface, field: Field, raw: string): Promise<string> {
  const rows = await optionRows($)
  const row = rows.find(r => r.key.endsWith(`.${FIELDS[field]}`))
  let value: string | number
  if (field === 'retention') {
    const listed = row?.options?.length ? row.options : RETENTION_OPTIONS
    const want = raw.trim().toLowerCase().replace(/\s+/g, ' ')
    const match = listed.find(o => o.toLowerCase() === want)
    if (!match) return `retention takes one of: ${listed.join(', ')}`
    value = match
  } else {
    value = parseTokens(raw)
    if (!Number.isFinite(value)) return `${field} takes a token count, such as 150000 or 150k`
  }
  if (!row) return await setDirect($, field, value)
  const { deny } = await $.config.set({ key: row.key, value })
  if (deny) return `Not changed: ${deny}`
  await loadSettings($)
  return `${field} set to ${shown(value)}. The proxy reads it on its next request.`
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
    if (showStatus) $.ui.status('restarting the proxy…')
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
    { PYTHONIOENCODING: 'utf-8' },
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

async function paneOpen($: EngineInterface, id = PANE) {
  return (await $.ui.panes()).some(p => p.id === id)
}

// Settings: a pane of their own, closed again by the same button. A press is
// the person asking, so the surface places it at any width; where it still is
// not placed, the box above the prompt shows them instead.
async function toggleSettings($: EngineInterface) {
  if (await paneOpen($)) {
    await $.ui.close({ id: PANE })
    // Closing writes no atom, so redraw for the button label by hand.
    $.ui.invalidate('ui.render')
    return
  }
  if (await read($, isOpen)) return update($, isOpen, () => false)
  await loadSettings($)
  const opened = await $.ui.open({
    id: PANE, title: 'Rolling context settings', focus: true, closeOnEscape: true, rows: 5,
  })
  // Opening writes no atom either; say "Hide settings" at once.
  $.ui.invalidate('ui.render')
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
  const retentionChoices = settings.retentionOptions.includes(settings.retention)
    ? settings.retentionOptions
    : [...settings.retentionOptions, settings.retention]
  return (
    <Box flexDirection="column" rowGap={1}>
      <Input
        key="trigger"
        label="Compact trigger (tokens) "
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
        options={retentionChoices.map(o => ({ value: o }))}
        value={settings.retention}
        onSelect={v => act($, setOption($, 'retention', v))}
      />
      <Text dimColor>Enter sets a number; the proxy reads it on its next request.</Text>
    </Box>
  )
}

// ---- Move session ----------------------------------------------------------

// move_session.py's answer, one JSON object; anything else is its failure.
async function moveScript($: EngineInterface, args: string[]): Promise<Record<string, unknown>> {
  const r = await runPython(
    $, [`${$.plugin.root}/proxy/move_session.py`, ...args], { PYTHONIOENCODING: 'utf-8' },
  )
  try {
    return JSON.parse(r.stdout) as Record<string, unknown>
  } catch {
    throw new Error(clean(r.stderr) || clean(r.stdout) || `move_session.py exited ${r.exitCode}`)
  }
}

// This session's record, the accounts and any move waiting; the pick stays
// while it is still another account, else the first other one.
async function loadMove($: EngineInterface) {
  const info = (await moveScript($, ['accounts', '--session', await $.session.id()])) as Move
  const here = info.session?.account
  await update($, moveNow, () => info)
  await update($, moveTo, cur =>
    cur && cur !== here && info.accounts.some(a => a.account === cur)
      ? cur
      : (info.accounts.find(a => a.account !== here)?.account ?? null))
  return info
}

async function retryMove($: EngineInterface) {
  if ((await read($, moveNow))?.session || moveTries <= 0) return
  moveTries--
  await loadMove($)
}

const accountLabel = (a: Account | undefined, id = '') =>
  a?.name || a?.email || `Account ${(a?.account ?? id).slice(0, 8)}`

function ago(ms: number, now: number) {
  const min = Math.max(0, Math.round((now - ms) / 60_000))
  return min < 60 ? `${min}m ago` : min < 48 * 60 ? `${Math.round(min / 60)}h ago` : `${Math.round(min / 1440)}d ago`
}

// What tells the accounts apart where no email is known: the latest session.
function accountDetail(a: Account, now: number) {
  const count = `${a.sessions} session${a.sessions === 1 ? '' : 's'}`
  if (!a.latestTitle) return count
  const when = a.latestAt ? `, ${ago(a.latestAt, now)}` : ''
  return `${count} · latest${a.latestArchived ? ' (archived)' : ''}: ${a.latestTitle}${when}`
}

async function nameAccount($: EngineInterface, account: string, text: string) {
  await moveScript($, ['name', '--account', account, '--name', text])
  await loadMove($)
  return text.trim() ? `Named it ${text.trim()}` : 'Name cleared'
}

// The app archives this session here, which stops its CLI: from then on only
// the other account opens the transcript. The finisher prepare started then
// removes the record here. Refused, or no app to ask (a terminal), the move
// waits: archiving the session by hand finishes it, Cancel move undoes it.
async function archiveHere($: EngineInterface, label: string) {
  if (working) return $.ui.toast('Stop the current turn first: a session moves once it is stopped')
  let said = ''
  try {
    const r = await $.mcp.call(APP_SESSIONS, 'archive_session', {
      session_id: 'self', reason: `Moving this session to ${label}`,
    })
    said = r.content.map(c => c.text ?? '').join(' ').trim()
    // Archived, this process ends within moments; still here, it was not.
    if (!r.isError) await $.clock.sleep(5_000)
  } catch (err) {
    said = String(err)
  }
  await loadMove($).catch(() => {})
  $.ui.toast(`Copied to ${label} but not archived here${said ? ` (${said})` : ''}: ` +
    'archive this session from the sidebar to finish, or Cancel move')
}

// With andSwitch, the app then switches to the account the session went to:
// switch_account.py waits for the move to finish before it quits the app.
async function startMove($: EngineInterface, andSwitch = false) {
  const info = await read($, moveNow)
  const to = await read($, moveTo)
  const pick = info?.accounts.find(a => a.account === to)
  if (!info?.session || !pick) return $.ui.toast('Pick an account to move this session to')
  if (working) return $.ui.toast('Stop the current turn first: a session moves once it is stopped')
  const label = accountLabel(pick)
  await update($, moveBusy, () => `copying it to ${label}…`)
  try {
    const id = await $.session.id()
    const out = await moveScript($, ['prepare', '--session', id, '--to', pick.account])
    if (!out.ok) return $.ui.toast(`Not moved: ${String(out.error ?? 'move_session.py refused')}`)
    if (andSwitch) {
      const sw = await switchScript($, ['switch', '--to', pick.account, '--after-move', id])
      $.ui.toast(sw.ok
        ? `Once it is archived here, the Claude app restarts signed in to ${label}`
        : `Moving without switching: ${String(sw.error ?? 'switch_account.py refused')}`)
    }
    await loadMove($)
    await update($, moveBusy, () => `archiving it here so ${label} can open it…`)
    $.ui.toast(`Copied to ${label}; archiving it here finishes the move`)
    await archiveHere($, label)
  } catch (err) {
    $.ui.toast(`Move session: ${String(err)}`)
  } finally {
    await update($, moveBusy, () => null).catch(() => {}) // the session may be ending
  }
}

async function cancelMove($: EngineInterface) {
  const out = await moveScript($, ['cancel', '--session', await $.session.id()])
  await loadMove($)
  return out.ok ? 'Move cancelled: the copy in the other account is gone' : String(out.error)
}

// ---- Switch account --------------------------------------------------------

// switch_account.py's answer, as moveScript reads move_session.py's.
async function switchScript($: EngineInterface, args: string[]): Promise<Record<string, unknown>> {
  const r = await runPython(
    $, [`${$.plugin.root}/proxy/switch_account.py`, ...args], { PYTHONIOENCODING: 'utf-8' },
  )
  try {
    return JSON.parse(r.stdout) as Record<string, unknown>
  } catch {
    throw new Error(clean(r.stderr) || clean(r.stdout) || `switch_account.py exited ${r.exitCode}`)
  }
}

const NEW_ACCOUNT = 'new'

// The account the app is signed in to: as its config.json says, else this
// session's.
const signedIn = (info: Move) => info.signedIn ?? info.session?.account ?? null

// What Switch account offers: every other account, then signing in to one
// it has not seen.
function switchChoices(info: Move) {
  const here = signedIn(info)
  return info.accounts.filter(a => a.account !== here)
}

async function switchPick($: EngineInterface, info: Move) {
  const choices = switchChoices(info)
  const cur = await read($, switchTo)
  if (cur === NEW_ACCOUNT || choices.some(a => a.account === cur)) return cur!
  return choices.find(a => a.login)?.account ?? choices[0]?.account ?? NEW_ACCOUNT
}

// Starts the detached switch and returns: the app quits within moments,
// taking this session's CLI with it.
async function startSwitch($: EngineInterface) {
  const info = await read($, moveNow)
  if (!info) return
  if (working) return $.ui.toast('Stop the current turn first: switching restarts the Claude app')
  const pick = await switchPick($, info)
  const acct = info.accounts.find(a => a.account === pick)
  const saved = !!acct?.login
  const label = acct ? accountLabel(acct) : 'another account'
  await update($, switchBusy, () => (saved ? `switching to ${label}…` : 'signing the app out…'))
  try {
    const out = await switchScript($, ['switch', '--to', saved ? pick : NEW_ACCOUNT])
    if (!out.ok) return $.ui.toast(`Not switched: ${String(out.error ?? 'switch_account.py refused')}`)
    $.ui.toast(saved
      ? `The Claude app restarts signed in to ${label}`
      : `The Claude app restarts signed out: sign in to ${label}`)
  } catch (err) {
    $.ui.toast(`Switch account: ${String(err)}`)
  } finally {
    await update($, switchBusy, () => null).catch(() => {}) // the app may be quitting
  }
}

async function forgetLogin($: EngineInterface, account: string) {
  const out = await switchScript($, ['forget', '--account', account])
  await loadMove($)
  return out.ok ? 'Saved login forgotten' : String(out.error)
}

const SWITCH_NOTE =
  'Switching restarts the Claude app: sessions that are working stop. The login of the account ' +
  'it leaves is saved first, so switching back needs no sign-in. Signing out in the app ends a ' +
  'saved login too, so switch here instead. Logins are saved in ~/.claude/rolling-context-logins ' +
  'and work only for this user on this computer.'

async function switchSection(
  $: EngineInterface,
  { Box, Button, Select, Text }: Elements['terminal'],
  info: Move,
) {
  const here = signedIn(info)
  const hereAcct = info.accounts.find(a => a.account === here)
  const choices = switchChoices(info)
  const pick = await switchPick($, info)
  const acct = choices.find(a => a.account === pick)
  const busy = await read($, switchBusy)
  const now = await $.clock.now()
  const label = acct ? accountLabel(acct) : ''
  const go = !acct ? 'Sign in to another account' : acct.login ? `Switch to ${label}` : `Sign in to ${label}`
  return (
    <Box flexDirection="column" rowGap={1}>
      <Text bold>Switch account</Text>
      <Text>{`Signed in to ${here ? accountLabel(hereAcct, here) : 'no account'}`}</Text>
      <Select
        key="switch-to"
        label="To "
        options={[
          ...choices.map(a => ({
            value: a.account,
            label: `${accountLabel(a)} · ${a.login ? 'login saved' : 'sign in once'} · ${accountDetail(a, now)}`,
          })),
          { value: NEW_ACCOUNT, label: 'Another account (sign in)' },
        ]}
        value={pick}
        onSelect={v => void update($, switchTo, () => v)}
      />
      {acct && !acct.login ? (
        <Text dimColor>
          {`No login of ${label} is saved yet: the app restarts signed out, you sign in to it once, and from then on it switches without signing in.`}
        </Text>
      ) : null}
      <Text dimColor>{SWITCH_NOTE}</Text>
      {working ? <Text color="warning">Stop the current turn first: switching restarts the Claude app.</Text> : null}
      <Box flexDirection="row" columnGap={1}>
        {working || busy ? null : <Button key="switch-go" label={go} onPress={() => void startSwitch($)} />}
        {acct?.login && !busy ? (
          <Button key="switch-forget" label="Forget saved login" onPress={() => act($, forgetLogin($, acct.account))} />
        ) : null}
      </Box>
      {busy ? <Text dimColor>{busy}</Text> : null}
    </Box>
  )
}

// ---- Import from another account -------------------------------------------

const shownSessions = (list: ImportList, archived: boolean) =>
  list.sessions.filter(s => archived || !s.archived)

// The account to import from (the one asked for, else the pick while it is
// still another account, else the first other one) and its sessions; the
// session picked stays while the list still shows it.
async function loadImport($: EngineInterface, account?: string) {
  const info = await read($, moveNow)
  const here = info?.session?.account
  const others = info?.accounts.filter(a => a.account !== here) ?? []
  const want = account ?? (await read($, importFrom))
  const from = others.find(a => a.account === want)?.account ?? others[0]?.account ?? null
  await update($, importFrom, () => from)
  if (!from) return
  const list = (await moveScript($, ['sessions', '--account', from])) as unknown as ImportList
  if (!list.ok) throw new Error(String((list as { error?: unknown }).error ?? 'move_session.py refused'))
  const shown = shownSessions(list, await read($, importArchived))
  await update($, importList, () => list)
  await update($, importPick, cur =>
    shown.some(s => s.localId === cur) ? cur : (shown[0]?.localId ?? null))
}

async function pickImportFrom($: EngineInterface, account: string) {
  await update($, importFrom, () => account)
  await loadImport($, account).catch(err => $.ui.toast(`Import: ${String(err)}`))
}

async function toggleArchived($: EngineInterface) {
  const archived = !(await read($, importArchived))
  await update($, importArchived, () => archived)
  const list = await read($, importList)
  const shown = list ? shownSessions(list, archived) : []
  await update($, importPick, cur =>
    shown.some(s => s.localId === cur) ? cur : (shown[0]?.localId ?? null))
}

const short = (text: string, n = 40) => (text.length > n ? `${text.slice(0, n - 1)}…` : text)

function sessionLabel(s: ImportSession, now: number) {
  const folder = (s.cwd ?? '').split(/[\\/]/).filter(Boolean).pop() ?? 'no folder'
  const when = s.lastActivityAt ? ago(s.lastActivityAt, now) : 'never active'
  return `${s.title || 'Untitled'} · ${when} · ${folder}${s.archived ? ' (archived)' : ''}` +
    `${s.blocked ? " · can't import" : ''}`
}

// Moves the picked session into this account at once: only the account
// signed in runs sessions, so nothing of the other one is running. The app
// lists it once it rereads this account's sessions.
async function importSession($: EngineInterface) {
  const info = await read($, moveNow)
  const from = await read($, importFrom)
  const list = await read($, importList)
  const localId = await read($, importPick)
  const pick = list?.account === from ? list?.sessions.find(s => s.localId === localId) : undefined
  if (!from || !pick) return $.ui.toast('Pick a session to import')
  const source = accountLabel(info?.accounts.find(a => a.account === from), from)
  const title = pick.title || 'the session'
  await update($, importBusy, () => `importing "${title}" from ${source}…`)
  try {
    const out = await moveScript($, [
      'import', '--session', await $.session.id(), '--from', from, '--local', pick.localId, '--open',
    ])
    if (!out.ok) return $.ui.toast(`Not imported: ${String(out.error ?? 'move_session.py refused')}`)
    const done = out.imported as { localId: string, opened?: boolean } | undefined
    const listed = done?.opened ? await keepTitle($, done.localId, pick.title) : false
    $.ui.toast(listed
      ? `Imported "${title}" from ${source}: it is in this account's sidebar now`
      : `Imported "${title}" from ${source}: restart the Claude app to see it in the sidebar`)
    await loadMove($)
    await loadImport($)
  } catch (err) {
    $.ui.toast(`Import: ${String(err)}`)
  } finally {
    await update($, importBusy, () => null)
  }
}

// The app adopts the session from the resume link within moments, naming it
// from its transcript; the title it had in the other account goes back on.
// False when the app never lists it (a terminal, or the link went nowhere).
async function keepTitle($: EngineInterface, localId: string, title: string) {
  for (let i = 0; i < 10; i++) {
    try {
      const r = await $.mcp.call(APP_SESSIONS, 'get_session', { session_id: localId })
      if (!r.isError) {
        const text = r.content.map(c => c.text ?? '').join('')
        if (title && !text.includes(JSON.stringify(title))) {
          await $.mcp.call(APP_SESSIONS, 'set_session_title', { session_id: localId, title })
            .catch(() => {})
        }
        return true
      }
    } catch {
      return false
    }
    await $.clock.sleep(1_000)
  }
  return false
}

const IMPORT_NOTE =
  'Importing moves it: it leaves that account and is listed under this one, live even if it ' +
  'was archived. Its transcript, scratchpad and rolling-context compressions stay on this ' +
  'computer under the same session id, so it carries on where it left off.'

async function importSection(
  $: EngineInterface,
  { Box, Button, Select, Text }: Elements['terminal'],
  others: Account[],
) {
  const now = await $.clock.now()
  const fromId = await read($, importFrom)
  const from = others.find(a => a.account === fromId)
  const list = await read($, importList)
  const fromSelect = (
    <Select
      key="import-from"
      label="From "
      options={others.map(a => ({ value: a.account, label: `${accountLabel(a)} · ${accountDetail(a, now)}` }))}
      value={from?.account}
      onSelect={v => void pickImportFrom($, v)}
    />
  )
  if (!from || list?.account !== from.account) {
    return (
      <Box flexDirection="column" rowGap={1}>
        <Text bold>Import from another account</Text>
        {fromSelect}
        <Text dimColor>Looking at its sessions…</Text>
      </Box>
    )
  }
  const archived = await read($, importArchived)
  const shown = shownSessions(list, archived)
  const archivedCount = list.sessions.filter(s => s.archived).length
  const pickId = await read($, importPick)
  const pick = shown.find(s => s.localId === pickId) ?? shown[0]
  const busy = await read($, importBusy)
  return (
    <Box flexDirection="column" rowGap={1}>
      <Text bold>Import from another account</Text>
      {fromSelect}
      {pick ? (
        <Select
          key="import-session"
          label="Session "
          options={shown.map(s => ({ value: s.localId, label: sessionLabel(s, now) }))}
          value={pick.localId}
          onSelect={v => void update($, importPick, () => v)}
        />
      ) : (
        <Text dimColor>
          {archivedCount ? 'It has only archived sessions here.' : 'It has no sessions on this computer.'}
        </Text>
      )}
      {pick ? <Text dimColor>{`In ${pick.cwd ?? 'no folder'}${pick.kind === 'folder' ? '' : ` (${pick.kind})`}`}</Text> : null}
      {pick?.blocked ? <Text color="warning">{pick.blocked}</Text> : <Text dimColor>{IMPORT_NOTE}</Text>}
      <Box flexDirection="row" columnGap={1}>
        {pick && !pick.blocked && !busy ? (
          <Button key="import-go" label={`Import "${short(pick.title || 'Untitled')}"`} onPress={() => void importSession($)} />
        ) : null}
        {archivedCount ? (
          <Button
            key="import-archived"
            label={archived ? 'Hide archived' : `Show archived (${archivedCount})`}
            onPress={() => void toggleArchived($)}
          />
        ) : null}
      </Box>
      {busy ? <Text dimColor>{busy}</Text> : null}
    </Box>
  )
}

// The pane, closed again by the same button; the box above the prompt where
// no pane is placed, as the settings do.
async function toggleMove($: EngineInterface) {
  if (await paneOpen($, MOVE_PANE)) {
    await $.ui.close({ id: MOVE_PANE })
    $.ui.invalidate('ui.render')
    return
  }
  if (await read($, moveOpen)) return update($, moveOpen, () => false)
  try {
    await loadMove($)
  } catch (err) {
    return $.ui.toast(`Sessions: ${String(err)}`)
  }
  void loadImport($).catch(err => $.ui.toast(`Import: ${String(err)}`))
  const opened = await $.ui.open({
    id: MOVE_PANE, title: 'Sessions', focus: true, closeOnEscape: true, rows: 20,
  })
  $.ui.invalidate('ui.render')
  if (!opened.isPlaced) {
    await $.ui.close({ id: MOVE_PANE })
    await update($, moveOpen, () => true)
  }
}

const MOVE_NOTE =
  'It is archived here and listed under that account: switch the Claude app to it (Switch ' +
  'account above, or Move and switch) and open it there. The transcript, scratchpad and ' +
  'rolling-context compressions stay on this computer under the same session id, so it ' +
  'carries on where it left off.'

// Switching the app's account, moving this session away, then importing one
// from another account, then Close.
async function moveBody($: EngineInterface, el: Elements['terminal']) {
  const { Box, Button, Text } = el
  const info = await read($, moveNow)
  if (!info) return <Text dimColor>Looking for this computer's accounts…</Text>
  const session = info.session
  if (!session) return <Text dimColor>{info.blocked ?? 'This session has no Claude app record.'}</Text>
  const others = info.accounts.filter(a => a.account !== session.account)
  return (
    <Box flexDirection="column" rowGap={1}>
      {await switchSection($, el, info)}
      {await moveSection($, el, info, session)}
      {others.length ? await importSection($, el, others) : null}
      <Button key="move-close" label="Close" onPress={() => void toggleMove($)} />
    </Box>
  )
}

async function moveSection(
  $: EngineInterface,
  { Box, Button, Input, Select, Text }: Elements['terminal'],
  info: Move,
  session: NonNullable<Move['session']>,
) {
  const busy = await read($, moveBusy)
  const byId = (id: string) => info.accounts.find(a => a.account === id)
  const head = (
    <Text bold>{`Move "${session.title || 'this session'}" from ${accountLabel(byId(session.account), session.account)}`}</Text>
  )
  const status = busy ? <Text dimColor>{busy}</Text> : null

  if (info.pending) {
    const to = accountLabel(byId(info.pending.to.account), info.pending.to.account)
    return (
      <Box flexDirection="column" rowGap={1}>
        {head}
        <Text>{`Copied to ${to}. The move finishes once this session is archived here; then switch the app to ${to} and open it there.`}</Text>
        <Box flexDirection="row" columnGap={1}>
          <Button key="move-archive" label="Archive here now" onPress={() => void archiveHere($, to)} />
          <Button key="move-cancel" label="Cancel move" onPress={() => act($, cancelMove($))} />
        </Box>
        {status}
      </Box>
    )
  }
  if (info.blocked) {
    return (
      <Box flexDirection="column" rowGap={1}>
        {head}
        <Text color="warning">{info.blocked}</Text>
      </Box>
    )
  }
  const others = info.accounts.filter(a => a.account !== session.account)
  if (!others.length) {
    return (
      <Box flexDirection="column" rowGap={1}>
        {head}
        <Text dimColor>No other account has used the Claude app on this computer.</Text>
      </Box>
    )
  }
  const pick = byId((await read($, moveTo)) ?? '') ?? others[0]!
  const now = await $.clock.now()
  return (
    <Box flexDirection="column" rowGap={1}>
      {head}
      <Select
        key="move-to"
        label="To "
        options={others.map(a => ({ value: a.account, label: `${accountLabel(a)} · ${accountDetail(a, now)}` }))}
        value={pick.account}
        onSelect={v => void update($, moveTo, () => v)}
      />
      <Input
        key={`move-name-${pick.account}`}
        label="Name this account "
        value={pick.name ?? ''}
        placeholder={pick.email ?? 'a name to tell it apart'}
        onSubmit={v => act($, nameAccount($, pick.account, v))}
      />
      <Text dimColor>{MOVE_NOTE}</Text>
      {working ? <Text color="warning">Stop the current turn first: a session moves once it is stopped.</Text> : null}
      {working || busy ? null : (
        <Box flexDirection="row" columnGap={1}>
          <Button key="move-go" label={`Move to ${accountLabel(pick)}`} onPress={() => void startMove($)} />
          {pick.login ? (
            <Button key="move-switch" label="Move and switch to it" onPress={() => void startMove($, true)} />
          ) : null}
        </Box>
      )}
      {status}
    </Box>
  )
}

// The band's Sessions button, for a session the app has a record of.
async function moveButton($: EngineInterface, { Button }: Elements['terminal']) {
  const info = showMove ? await read($, moveNow) : null
  if (!info?.session) return null
  const shown = (await read($, moveOpen)) || (await paneOpen($, MOVE_PANE))
  const label = shown ? 'Hide sessions' : info.pending ? 'Sessions (move pending)' : 'Sessions'
  return <Button key="move" label={label} onPress={() => void toggleMove($)} />
}

// The status, bold in the theme's text color, then the buttons in
// Toggle/Restart/Settings order; `hint`, the engine's own line where the row
// replaces it, dim after them.
async function controlsRow($: EngineInterface, { Box, Button, Text }: Elements['terminal'], hint?: string) {
  const now = await read($, healthNow) // not `h`: that is the JSX factory
  const busy = await read($, busyNow)
  const ctx = await read($, ctxNow)
  const shown = (await read($, isOpen)) || (await paneOpen($))
  const toggle = toggleAction(now)
  return (
    <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
      {showStatus ? (
        <Text bold color="text">{`Rolling context: ${busy ?? statusText(now, ctx)}`}</Text>
      ) : null}
      {showControls ? (
        <Box flexDirection="row" columnGap={1}>
          <Button key="toggle" label={toggle.label} onPress={() => act($, run($, toggle.args))} />
          {now ? (
            <Button key="restart" label="Restart" onPress={() => act($, run($, 'restart'))} />
          ) : null}
          <Button
            key="settings"
            label={shown ? 'Hide settings' : 'Settings'}
            onPress={() => toggleSettings($)}
          />
        </Box>
      ) : null}
      {hint ? <Text dimColor>{hint}</Text> : null}
    </Box>
  )
}

// What this module has seen, kept in ~/.claude/rolling-context-ui.json: when
// and from where it last loaded, how its last /health went, and which of its
// sites each surface has asked it to draw (hosts differ: the desktop app
// never asks for the row under the prompt). Where no status line shows, this
// tells the causes apart, which the proxy's log cannot (it is silent for
// both): no file or a stale `loaded`, the host never loaded the module; a
// failing `health`, it loaded and cannot reach the proxy.
const sitesSeen = new Set<string>()
const diag = { loaded: '', health: '' }
function writeDiag($: EngineInterface) {
  $.clock.sleep(0).then(async () => {
    await $.fs.write(`${await home($)}/.claude/rolling-context-ui.json`,
      JSON.stringify({ ...diag, sites: [...sitesSeen].sort() }, null, 1))
  }).catch(() => {}) // a diagnostic only
}

function seen($: EngineInterface, e: { component: string; surface: string }) {
  const site = `${e.surface}:${e.component}`
  if (sitesSeen.has(site)) return
  sitesSeen.add(site)
  writeDiag($)
}

// Written when the outcome changes, not on every 30 s poll.
function noteHealth($: EngineInterface, outcome: string): undefined {
  if (outcome !== diag.health) {
    diag.health = outcome
    writeDiag($)
  }
  return undefined
}

// A command hook that throws would otherwise fall through to the .md
// command (a Claude turn) or to "no hook answered": say what went wrong.
function failed(_$: unknown, _e: unknown, next: { error?: { message?: string } }) {
  return { text: `rolling-context: ${next.error?.message ?? 'the command failed'}` }
}

export const register: Register = (on, options) => {
  showStatus = options.status_line !== false
  showControls = options.controls !== false
  showMove = options.move_button !== false

  on('session.start', async ($, e, next) => {
    diag.loaded = `${new Date(await $.clock.now()).toISOString()} from ${$.plugin.root}`
    writeDiag($)
    // Background refresh: a session that ends mid-refresh has nothing to
    // update, so a late write failing there is dropped, not reported.
    void refreshStatus($).catch(() => {})
    void optionRows($).catch(() => {}) // records which pluginConfigs entry is live
    // Whether the app has a record of this session, for the Move button.
    if (showMove) void loadMove($).catch(() => {})
    $.clock.every(STATUS_EVERY_MS, () => {
      void refreshStatus($)
      if (showMove) void retryMove($).catch(() => {})
    })
    if (!showStatus) $.ui.status(undefined)
    // /roll is the plugin's only command. A name another command already
    // holds (a built-in or its alias, as /rc is /remote-control's) is refused
    // with a throw; say so, since the controls row still works without it.
    try {
      await $.command.register({
        name: 'roll',
        description: 'rolling-context: status, on/off for this session (--global for all), set an option, or restart the proxy',
        argumentHint: '[status|on|off|restart|set <option> <value>] [--global]',
        immediate: true,
      })
    } catch (err) {
      $.ui.log(`/roll is unavailable (${String(err)}); the controls under the prompt still work`)
    }
    return next(e)
  })

  on('command.run', { command: 'roll' }, async ($, e) => ({ text: await run($, e.args) }))
    .catch(failed)

  // A turn may have triggered a compression; show it without waiting 30 s.
  // The main loop's turns also say whether the session is stopped, which a
  // move waits for; the pane redraws to say so.
  on('turn.complete', async ($, e, next) => {
    void refreshStatus($).catch(() => {})
    if ((e as { agentId?: string }).agentId === undefined && working) {
      working = false
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if ((e as { agentId?: string }).agentId === undefined && !working) {
      working = true
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  // The row under the prompt: the status, bold in the theme's text color,
  // then the buttons in Toggle/Restart/Settings order; the engine's own hint
  // (`? for shortcuts`) follows, dim, so nothing of it is lost. The Claude
  // desktop app draws that footer itself and never asks for it; there the
  // band above the prompt carries one compact line instead (below). The
  // status line under the prompt keeps the status either way.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    seen($, e)
    if (!showStatus && !showControls) return next(e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    if (!rowDrawn) {
      rowDrawn = true
      // The band may have drawn its compact line before this row existed; a
      // draw never writes, so ask for another pass from the next tick and the
      // band drops its copy.
      $.clock.sleep(0).then(() => $.ui.invalidate('ui.render')).catch(() => {}) // unloaded before it came due
    }
    return controlsRow($, $.ui.resolve(e) as Elements['terminal'], e.props.hint)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    seen($, e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    // Loaded by the Settings button before it opens the pane; a draw never writes.
    const settings = await read($, settingsNow)
    const els = $.ui.resolve(e) as Elements['terminal']
    if (!settings) return <els.Text dimColor>Loading the settings…</els.Text>
    return settingsBody($, els, settings)
  })

  on('ui.render', { component: 'Pane', requestId: MOVE_PANE }, async ($, e, next) => {
    seen($, e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    return moveBody($, $.ui.resolve(e) as Elements['terminal'])
  })

  // A pane can close without its button (Esc, its close mark, or the engine
  // dropping it): pass the close on, then redraw so the button says
  // "Settings" or "Sessions" again. A hook that answers without `next`
  // would keep the pane open, so the close always passes through.
  on('ui.close', async ($, e, next) => {
    if (e.id !== PANE && e.id !== MOVE_PANE) return next(e)
    const out = await next(e)
    $.ui.invalidate('ui.render')
    return out
  })

  // The band above the prompt: one compact line ("RC" and the buttons in
  // Toggle/Restart/Settings order) where the surface draws no row under the
  // prompt (the desktop app), and the settings, toggled by the Settings
  // button, where the pane could not be placed. The status itself lives in
  // the status line, so the band costs a single line; elsewhere it stays
  // empty apart from the settings fallback. Other mods' band drawings are
  // kept above ours.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    seen($, e)
    if (e.props.hasSurvey) return next(e)
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    // The band is redrawn as a turn starts and ends: the freshest word on it.
    working = e.props.isWorking
    const els = $.ui.resolve(e) as Elements['terminal']
    const row = !rowDrawn && showControls
    const settings = (await read($, isOpen)) ? await read($, settingsNow) : null
    const move = await moveButton($, els)
    const moveShown = await read($, moveOpen)
    if (!row && !settings && !move && !moveShown) return next(e)
    const theirs = await next(e)
    const { Box, Button, Text } = els
    const now = row ? await read($, healthNow) : null // not `h`: that is the JSX factory
    const toggle = row ? toggleAction(now) : null
    const shown = row ? (await read($, isOpen)) || (await paneOpen($)) : false
    // Sessions sits at the end of the RC line, or on a line of its own
    // where the row under the prompt holds the RC buttons.
    return (
      <Box flexDirection="column">
        {theirs}
        {row && toggle ? (
          <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
            <Text dimColor>RC</Text>
            <Button key="toggle" label={toggle.label} onPress={() => act($, run($, toggle.args))} />
            {now ? (
              <Button key="restart" label="Restart" onPress={() => act($, run($, 'restart'))} />
            ) : null}
            <Button
              key="settings"
              label={shown ? 'Hide settings' : 'Settings'}
              onPress={() => toggleSettings($)}
            />
            {move}
          </Box>
        ) : move ? (
          <Box flexDirection="row">{move}</Box>
        ) : null}
        {settings ? settingsBody($, els, settings) : null}
        {moveShown ? await moveBody($, els) : null}
      </Box>
    )
  })
}
