// Tests for the Sessions pane in hooks/register.tsx, run by `claude plugin test`.
// move_session.py, switch_account.py and the app's archive_session are
// answered beneath the mod; the scripts' own behaviour is
// tests/test_move_session.py's and tests/test_switch_account.py's.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1234'
const HERE = 'aaaaaaaa-0000-0000-0000-000000000001'
const WORK = 'bbbbbbbb-0000-0000-0000-000000000002'
const OLD = 'cccccccc-0000-0000-0000-000000000003'

type Info = Record<string, unknown>

function accounts(over: Info = {}): Info {
  return {
    ok: true,
    session: {
      account: HERE, org: 'o1', localId: 'local_x', title: 'The session', cwd: '/work',
      kind: 'folder', transcript: '/t.jsonl',
    },
    accounts: [
      { account: HERE, org: 'o1', email: 'me@example.com', name: null, sessions: 3, latestTitle: 'The session', latestAt: 0 },
      { account: WORK, org: 'o2', email: 'work@example.com', name: null, sessions: 2, latestTitle: 'Bot', latestAt: 0, login: true },
      { account: OLD, org: 'o3', email: null, name: null, sessions: 0, latestTitle: 'Gone', latestAt: 0, latestArchived: true },
    ],
    signedIn: HERE,
    pending: null,
    blocked: null,
    ...over,
  }
}

function session(localId: string, over: Info = {}): Info {
  return {
    localId, cliSessionId: `cli-${localId}`, org: 'o2', title: `Session ${localId}`, cwd: 'C:\\work\\repo',
    kind: 'folder', archived: false, lastActivityAt: 1_000_000 - 5 * 60_000, blocked: null, ...over,
  }
}

// Each other account's sessions, as `sessions --account` answers.
function lists(): Record<string, Info[]> {
  return {
    [WORK]: [
      session('local_w1', { title: 'Bot' }),
      session('local_w2', { title: 'No transcript', blocked: 'Its transcript is not on this computer.' }),
      session('local_w3', { title: 'Old work', archived: true, cwd: '/home/u/old' }),
    ],
    [OLD]: [session('local_o1', { title: 'Gone', archived: true, org: 'o3' })],
  }
}

// move_session.py answers by its verb; `info` is what `accounts` says now.
function world(on: On, first: Info = accounts(), archive: { isError: boolean; text: string } = { isError: false, text: 'Archived' }) {
  const runs: string[][] = []
  const switches: string[][] = []
  const calls: { server: string; tool: string; args: Record<string, unknown> }[] = []
  const toasts: string[] = []
  const state = {
    info: first, lists: lists(),
    importAnswer: { ok: true, imported: { localId: 'local_cli-w1', opened: true } } as Info,
    switchAnswer: { ok: true } as Info,
    // What the app's get_session says of the imported session: listed, under its transcript's name.
    adopted: { isError: false, text: JSON.stringify({ sessionId: 'local_cli-w1', title: 'hello there' }) },
  }
  mock.env(on, { HOME: '/home/u' })
  const clock = mock.clock(on, { now: 1_000_000 })
  on('session.id', async () => ({ value: SESSION }))
  on('session.usage', async () => ({ deny: 'none in tests' }))
  on('http.fetch', async () => ({ deny: 'no proxy in tests' }))
  on('process.run', async ($, e) => {
    const script = e.argv[1] ?? ''
    const args = e.argv.slice(2)
    let out: Info = { ok: true }
    if (script.endsWith('switch_account.py')) {
      switches.push([...args])
      if (args[0] === 'switch') out = state.switchAnswer
    }
    if (script.endsWith('move_session.py')) {
      runs.push([...args])
      if (args[0] === 'accounts') out = state.info
      if (args[0] === 'prepare') {
        state.info = { ...state.info, pending: { state: 'pending', at: 0, title: 'The session', from: { account: HERE, org: 'o1' }, to: { account: args[4], org: 'o2' } } }
      }
      if (args[0] === 'cancel') state.info = { ...state.info, pending: null }
      if (args[0] === 'sessions') out = { ok: true, account: args[2], sessions: state.lists[args[2]!] ?? [] }
      if (args[0] === 'import') {
        out = state.importAnswer
        if (out.ok) state.lists[args[4]!] = (state.lists[args[4]!] ?? []).filter(s => s.localId !== args[6])
      }
    }
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('mcp.call', async ($, e) => {
    calls.push({ server: e.server, tool: e.tool, args: e.args })
    const answer = e.tool === 'get_session' ? state.adopted : e.tool === 'set_session_title' ? { isError: false, text: 'ok' } : archive
    return { value: { content: [{ type: 'text', text: answer.text }], isError: answer.isError } }
  })
  on('ui.toast', async ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', async () => ({ value: undefined }))
  on('ui.log', async () => ({ value: undefined }))
  on('ui.invalidate', async () => ({ value: undefined }))
  on('fs.write', async () => ({ value: undefined }))
  on('fs.read', async () => ({ deny: 'none' }))
  on('config.list', async () => ({ value: [] }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  const open = new Set<string>()
  on('ui.open', async ($, e) => {
    open.add(e.id)
    return { value: { isPlaced: true as const } }
  })
  on('ui.close', async ($, e) => {
    open.delete(e.id)
    return { value: undefined }
  })
  on('ui.panes', async () => ({ value: [...open].map(id => ({ id, title: id })) }))
  on('ui.render', async ($, e) => h($.ui.resolve(e).Box, {})) // the engine's own band: empty
  return { runs, switches, calls, toasts, open, state, clock }
}

type T = Parameters<Parameters<typeof test>[1]>[0]

async function start($: T, on: On) {
  on('session.start', async ($, e) => e)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
}

// A press starts the move unawaited; let it run, past the 5 s it gives the
// archive to end this process.
async function settle(w: { clock: { advance: (ms: number) => Promise<void> } }) {
  await w.clock.advance(6_000)
}

const band = ($: T, isWorking = false) => $.ui.mount({
  plugin: 'rolling-context', surface: 'desktop', component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking, maxRows: 20, bodyColumns: 100, scroll: { offset: 0, bodyRows: 20 }, view: {} },
})

const pane = ($: T) => $.ui.mount({
  plugin: 'rolling-context', surface: 'desktop', component: 'Pane', requestId: 'move',
  props: { title: 'Sessions', bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as Parameters<typeof $.ui.mount>[0])

describe('Sessions', () => {
  test('sits at the end of the RC line in a session the app has a record of', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    expect((await b.find({ key: 'settings' }))?.text).toMatch(/Settings/)
    expect((await b.find({ key: 'move' }))?.text).toBe('Sessions')
    expect(w.runs[0]).toEqual(['accounts', '--session', SESSION])
  })

  test('is not offered in a session the app has no record of', async ($, on) => {
    const w = world(on, accounts({ session: null, accounts: [], blocked: 'no record' }))
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    expect(await b.find({ key: 'move' })).toBeUndefined()
  })

  test('opens a pane offering the other accounts, then moves and asks the app to archive it here', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    expect(w.open.has('move')).toBe(true)
    await b.redraw()
    expect((await b.find({ key: 'move' }))?.text).toBe('Hide sessions')

    const p = await pane($)
    const to = await p.find({ key: 'move-to' })
    const options = (to?.props.options ?? []) as { value: string; label: string }[]
    expect(options.map(o => o.value)).toEqual([WORK, OLD])            // not the account it is in
    expect(options[0]!.label).toMatch(/^work@example\.com · 2 sessions · latest: Bot/)
    expect(options[1]!.label).toMatch(/^Account cccccccc · 0 sessions · latest \(archived\): Gone/)
    expect((await p.find({ key: 'move-go' }))?.text).toBe('Move to work@example.com')

    await p.select({ key: 'move-to', value: OLD })
    expect((await p.find({ key: 'move-go' }))?.text).toBe('Move to Account cccccccc')
    await p.select({ key: 'move-to', value: WORK })
    await p.press({ key: 'move-go' })
    await settle(w)
    expect(w.runs.find(r => r[0] === 'prepare')).toEqual(['prepare', '--session', SESSION, '--to', WORK])
    expect(w.calls).toEqual([{
      server: 'ccd_session_mgmt', tool: 'archive_session',
      args: { session_id: 'self', reason: 'Moving this session to work@example.com' },
    }])
    expect(w.toasts[0]).toMatch(/Copied to work@example\.com; archiving it here finishes the move/)
  })

  test('waits while a turn runs', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($, true)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    expect(await p.find({ key: 'move-go' })).toBeUndefined()
    expect((await p.find({ type: 'Text', text: /Stop the current turn first/ }))?.props.color).toBe('warning')
    expect(w.runs.some(r => r[0] === 'prepare')).toBe(false)
  })

  test('an archive the app refuses leaves the move waiting, with Cancel move', async ($, on) => {
    const w = world(on, accounts(), { isError: true, text: 'open on screen' })
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.press({ key: 'move-go' })
    await settle(w)
    expect(w.toasts.at(-1)).toMatch(/Copied to work@example\.com but not archived here \(open on screen\)/)
    await p.redraw()
    expect((await p.find({ type: 'Text', text: /^Copied to work@example\.com\. The move finishes/ }))).toBeDefined()
    await b.redraw()
    expect((await b.find({ key: 'move' }))?.text).toBe('Hide sessions')
    await p.press({ key: 'move-cancel' })
    await settle(w)
    expect(w.runs.at(-2)).toEqual(['cancel', '--session', SESSION])
    expect(w.toasts.at(-1)).toMatch(/Move cancelled/)
    await p.redraw()
    expect(await p.find({ key: 'move-go' })).toBeDefined()
  })

  test('a blocked session (a worktree) says why, with nothing to press', async ($, on) => {
    const w = world(on, accounts({ blocked: 'This session runs in a worktree' }))
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    expect((await p.find({ type: 'Text', text: /runs in a worktree/ }))?.props.color).toBe('warning')
    expect(await p.find({ key: 'move-go' })).toBeUndefined()
  })

  test('imports a session of another account into this one', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    expect(w.runs).toContainEqual(['sessions', '--account', WORK])

    const p = await pane($)
    const from = await p.find({ key: 'import-from' })
    expect(((from?.props.options ?? []) as { value: string }[]).map(o => o.value)).toEqual([WORK, OLD])
    expect(from?.props.value).toBe(WORK)
    const list = await p.find({ key: 'import-session' })
    const options = (list?.props.options ?? []) as { value: string; label: string }[]
    expect(options.map(o => o.value)).toEqual(['local_w1', 'local_w2'])      // archived ones hidden
    expect(options[0]!.label).toBe('Bot · 5m ago · repo')
    expect(options[1]!.label).toMatch(/· can't import$/)
    expect((await p.find({ type: 'Text', text: /^In C:\\work\\repo$/ }))).toBeDefined()
    expect((await p.find({ key: 'import-go' }))?.text).toBe('Import "Bot"')

    await p.press({ key: 'import-go' })
    await settle(w)
    expect(w.runs).toContainEqual(['import', '--session', SESSION, '--from', WORK, '--local', 'local_w1', '--open'])
    expect(w.toasts.at(-1)).toBe(`Imported "Bot" from work@example.com: it is in this account's sidebar now`)
    // The app named it from its transcript; the title it had goes back on.
    expect(w.calls).toContainEqual({
      server: 'ccd_session_mgmt', tool: 'set_session_title', args: { session_id: 'local_cli-w1', title: 'Bot' },
    })
    await p.redraw()
    const after = (await p.find({ key: 'import-session' }))?.props.options as { value: string }[]
    expect(after.map(o => o.value)).toEqual(['local_w2'])                     // reloaded: it left WORK
  })

  test('an import the app does not list at once says to restart it', async ($, on) => {
    const w = world(on)
    w.state.adopted = { isError: true, text: 'Session local_cli-w1 not found.' }
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.press({ key: 'import-go' })
    await w.clock.advance(15_000)                                            // ten tries, a second apart
    expect(w.toasts.at(-1)).toBe('Imported "Bot" from work@example.com: restart the Claude app to see it in the sidebar')
    expect(w.calls.filter(c => c.tool === 'get_session')).toHaveLength(10)
    expect(w.calls.find(c => c.tool === 'set_session_title')).toBeUndefined()
  })

  test('a session without a transcript here cannot be imported, and says why', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.select({ key: 'import-session', value: 'local_w2' })
    expect((await p.find({ type: 'Text', text: /transcript is not on this computer/ }))?.props.color).toBe('warning')
    expect(await p.find({ key: 'import-go' })).toBeUndefined()
  })

  test('another account and its archived sessions can be shown', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.press({ key: 'import-archived' })
    await settle(w)
    await p.redraw()
    let options = (await p.find({ key: 'import-session' }))?.props.options as { value: string; label: string }[]
    expect(options.map(o => o.value)).toEqual(['local_w1', 'local_w2', 'local_w3'])
    expect(options[2]!.label).toBe('Old work · 5m ago · old (archived)')
    expect((await p.find({ key: 'import-archived' }))?.text).toBe('Hide archived')

    await p.select({ key: 'import-from', value: OLD })
    await settle(w)
    await p.redraw()
    expect(w.runs).toContainEqual(['sessions', '--account', OLD])
    options = (await p.find({ key: 'import-session' }))?.props.options as { value: string; label: string }[]
    expect(options.map(o => o.value)).toEqual(['local_o1'])
    expect((await p.find({ key: 'import-go' }))?.text).toBe('Import "Gone"')
  })

  test('an import move_session.py refuses says why', async ($, on) => {
    const w = world(on)
    w.state.importAnswer = { ok: false, error: 'This account already has that session.' }
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.press({ key: 'import-go' })
    await settle(w)
    expect(w.toasts.at(-1)).toBe('Not imported: This account already has that session.')
  })

  test('an account can be named', async ($, on) => {
    const w = world(on)
    await start($, on)
    await w.clock.settle()
    const b = await band($)
    await b.press({ key: 'move' })
    await settle(w)
    const p = await pane($)
    await p.input({ key: `move-name-${WORK}`, text: 'Work' })
    await settle(w)
    expect(w.runs.find(r => r[0] === 'name')).toEqual(['name', '--account', WORK, '--name', 'Work'])
  })
})

describe('Switch account', () => {
  async function open($: T, on: On, first: Info = accounts(), isWorking = false) {
    const w = world(on, first)
    await start($, on)
    await w.clock.settle()
    const b = await band($, isWorking)
    await b.press({ key: 'move' })
    await settle(w)
    return { w, p: await pane($) }
  }

  test('says which account the app is in and offers the others, a saved login first', async ($, on) => {
    const { w, p } = await open($, on)
    expect(await p.find({ type: 'Text', text: 'Signed in to me@example.com' })).toBeDefined()
    const to = await p.find({ key: 'switch-to' })
    const options = (to?.props.options ?? []) as { value: string; label: string }[]
    expect(options.map(o => o.value)).toEqual([WORK, OLD, 'new'])
    expect(options[0]!.label).toMatch(/^work@example\.com · login saved · 2 sessions/)
    expect(options[1]!.label).toMatch(/^Account cccccccc · sign in once · /)
    expect(options[2]!.label).toBe('Another account (sign in)')
    expect(to?.props.value).toBe(WORK)
    expect((await p.find({ key: 'switch-go' }))?.text).toBe('Switch to work@example.com')

    await p.press({ key: 'switch-go' })
    await settle(w)
    expect(w.switches).toEqual([['switch', '--to', WORK]])
    expect(w.toasts.at(-1)).toBe('The Claude app restarts signed in to work@example.com')
  })

  test('an account with no saved login is signed in to once, from a signed-out app', async ($, on) => {
    const { w, p } = await open($, on)
    await p.select({ key: 'switch-to', value: OLD })
    expect((await p.find({ key: 'switch-go' }))?.text).toBe('Sign in to Account cccccccc')
    expect(await p.find({ type: 'Text', text: /No login of Account cccccccc is saved yet/ })).toBeDefined()
    expect(await p.find({ key: 'switch-forget' })).toBeUndefined()
    await p.press({ key: 'switch-go' })
    await settle(w)
    expect(w.switches).toEqual([['switch', '--to', 'new']])
    expect(w.toasts.at(-1)).toBe('The Claude app restarts signed out: sign in to Account cccccccc')

    await p.select({ key: 'switch-to', value: 'new' })
    expect((await p.find({ key: 'switch-go' }))?.text).toBe('Sign in to another account')
  })

  test('the signed-in account is left out even when this session is filed under another', async ($, on) => {
    const { p } = await open($, on, accounts({ signedIn: WORK }))
    expect(await p.find({ type: 'Text', text: 'Signed in to work@example.com' })).toBeDefined()
    const options = (await p.find({ key: 'switch-to' }))?.props.options as { value: string }[]
    expect(options.map(o => o.value)).toEqual([HERE, OLD, 'new'])
  })

  test('waits while a turn runs', async ($, on) => {
    const { w, p } = await open($, on, accounts(), true)
    expect(await p.find({ key: 'switch-go' })).toBeUndefined()
    expect(w.switches).toEqual([])
  })

  test('a switch switch_account.py refuses says why', async ($, on) => {
    const { w, p } = await open($, on)
    w.state.switchAnswer = { ok: false, error: 'This session is not running in the Claude desktop app.' }
    await p.press({ key: 'switch-go' })
    await settle(w)
    expect(w.toasts.at(-1)).toBe('Not switched: This session is not running in the Claude desktop app.')
  })

  test('a saved login can be forgotten', async ($, on) => {
    const { w, p } = await open($, on)
    await p.press({ key: 'switch-forget' })
    await settle(w)
    expect(w.switches).toEqual([['forget', '--account', WORK]])
    expect(w.toasts.at(-1)).toBe('Saved login forgotten')
  })

  test('Move and switch moves, then has the switch wait for the move', async ($, on) => {
    const { w, p } = await open($, on)
    await p.select({ key: 'move-to', value: OLD })              // no saved login: nothing to switch to
    expect(await p.find({ key: 'move-switch' })).toBeUndefined()
    await p.select({ key: 'move-to', value: WORK })
    await p.press({ key: 'move-switch' })
    await settle(w)
    expect(w.runs.find(r => r[0] === 'prepare')).toEqual(['prepare', '--session', SESSION, '--to', WORK])
    expect(w.switches).toEqual([['switch', '--to', WORK, '--after-move', SESSION]])
    expect(w.calls.map(c => c.tool)).toEqual(['archive_session'])
    expect(w.toasts[0]).toBe('Once it is archived here, the Claude app restarts signed in to work@example.com')

  })
})
