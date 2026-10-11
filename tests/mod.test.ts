// Tests for hooks/register.tsx, run by `claude plugin test`.
// Nothing here touches a real process, the network or the disk: the test's
// hooks answer $.process.run, $.http.fetch and $.session.id beneath the mod.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1234'

type Run = { argv: readonly string[]; env: Record<string, string> }

// hangFetch: /health never answers, as a fetch through a proxy that cannot
// relay it did.
function world(on: On, os: 'Windows_NT' | 'Linux', stdout: string, hangFetch = false) {
  const runs: Run[] = []
  const statuses: (string | undefined)[] = []
  mock.env(on, os === 'Windows_NT'
    ? { OS: 'Windows_NT', USERPROFILE: 'C:/Users/u' }
    : { HOME: '/home/u' })
  const clock = mock.clock(on)
  on('session.id', async () => ({ value: SESSION }))
  on('process.run', async ($, e) => {
    runs.push({ argv: e.argv, env: { ...(e.init?.env ?? {}) } })
    return { value: { exitCode: 0, stdout, stderr: '' } }
  })
  on('http.fetch', async () => hangFetch ? new Promise<never>(() => {}) : ({
    value: {
      status: 200, ok: true, headers: {},
      text: JSON.stringify({
        service: 'rolling-context', enabled: true, total_tokens_saved: 4_700_000,
        stored_compressions: 40, active_compressions: 0,
        session: { enabled: false, scope: 'this session' },
      }),
    },
  }))
  on('ui.status', async ($, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  return { runs, statuses, clock }
}

describe('/roll', () => {
  test('off runs switch.py for this session and prints its output, minus the marker', async ($, on) => {
    const w = world(on, 'Linux', '<<rolling-context:session-off>>\nrolling-context is now OFF')
    const { text } = await $.command.run({ command: 'roll', args: 'off' })
    expect(text).toBe('rolling-context is now OFF')
    const run = w.runs.at(-1)!
    expect(run.argv[0]).toBe('python3')
    expect(run.argv.slice(2)).toEqual(['off', '--session', SESSION])
    expect(run.argv[1]).toMatch(/proxy\/switch\.py$/)
    expect(run.env.ROLLING_CONTEXT_VIA).toBe('mod')
  })

  test('uses python on Windows and passes --global through', async ($, on) => {
    const w = world(on, 'Windows_NT', 'rolling-context is now OFF on this machine')
    await $.command.run({ command: 'roll', args: 'off --global' })
    const run = w.runs.at(-1)!
    expect(run.argv[0]).toBe('python')
    expect(run.argv.slice(2)).toEqual(['off', '--global', '--session', SESSION])
  })

  test('an unknown action says how to use it, without running anything', async ($, on) => {
    const w = world(on, 'Linux', '')
    const { text } = await $.command.run({ command: 'roll', args: 'sideways' })
    expect(text).toMatch(/Usage: \/roll/)
    expect(w.runs.length).toBe(0)
  })

  test('a /health fetch that never answers does not hold the command open', async ($, on) => {
    const { clock } = world(on, 'Linux', 'rolling-context is now OFF', true)
    let done = false
    const pending = $.command.run({ command: 'roll', args: 'off' }).then(r => {
      done = true
      return r
    })
    await clock.advance(1_000)
    expect(done).toBe(false)
    await clock.advance(1_500)
    expect(done).toBe(true)
    expect((await pending).text).toBe('rolling-context is now OFF')
  })

  test('the status line shows this session\'s state after a toggle', async ($, on) => {
    const w = world(on, 'Linux', 'ok')
    await $.command.run({ command: 'roll', args: 'off' })
    expect(w.statuses.at(-1)).toBe('off for this session · 4.7M tokens saved · 40 stored')
  })
})

test('the plugin\'s own /rolling-context:off is answered without a Claude turn', async ($, on) => {
  const w = world(on, 'Linux', 'rolling-context is now OFF')
  const { text } = await $.command.run({ command: 'rolling-context:off', args: '' })
  expect(text).toBe('rolling-context is now OFF')
  expect(w.runs.at(-1)!.argv.slice(2)).toEqual(['off', '--session', SESSION])
})

describe('registration', () => {
  // What the menu shows for the plugin's own /rolling-context:status.
  async function legacyHidden($: Parameters<Parameters<typeof test>[1]>[0], on: On) {
    on('command.describe', async ($, e) => ({ description: e.description, isHidden: e.isHidden }))
    on('ui.log', async () => ({ value: undefined }))
    on('session.start', async ($, e) => e)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    const shown = await $.command.describe({
      command: 'rolling-context:status', description: 'status', isHidden: false,
      immediate: false, provider: { plugin: 'rolling-context', tier: 'user' },
    })
    return shown.isHidden
  }

  test('/roll registers, and the .md commands are hidden in its favour', async ($, on) => {
    world(on, 'Linux', '')
    on('command.register', async ($, e) => ({ value: { command: e.name } }))
    expect(await legacyHidden($, on)).toBe(true)
  })

  test('if /roll is refused (a taken name), the .md commands stay listed', async ($, on) => {
    world(on, 'Linux', '')
    on('command.register', async () => ({ deny: '"/roll" refused: it is taken' }))
    expect(await legacyHidden($, on)).toBe(false)
  })
})

// The plugin's /config rows, answered beneath the mod; what it set is kept.
function config(on: On) {
  const values: Record<string, number | string> = {
    'rolling-context.trigger_tokens': 200_000,
    'rolling-context.target_tokens': 100_000,
    'rolling-context.retention': '14 days',
  }
  const sets: { key: string; value: unknown }[] = []
  on('config.list', async () => ({
    value: Object.entries(values).map(([key, value]) => ({
      key, label: key, value,
      kind: typeof value === 'number' ? ('number' as const) : ('choice' as const),
      options: key.endsWith('.retention') ? ['1 day', '14 days', 'Never'] : undefined,
      provider: { plugin: 'rolling-context', tier: 'user' as const },
      isLocked: false,
    })),
  }))
  on('config.set', async ($, e) => {
    sets.push({ key: e.key, value: e.value })
    values[e.key] = e.value as number | string
    return { value: e.value }
  })
  return sets
}

// A session started as the engine starts one, /roll registered.
async function start($: Parameters<Parameters<typeof test>[1]>[0], on: On) {
  on('ui.log', async () => ({ value: undefined }))
  on('ui.toast', async () => ({ value: undefined }))
  on('ui.invalidate', async () => ({ value: undefined }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('session.start', async ($, e) => e)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
}

describe('/roll set', () => {
  test('sets a token count in /config, k suffix and all', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on)
    const { text } = await $.command.run({ command: 'roll', args: 'set trigger 150k' })
    expect(sets).toEqual([{ key: 'rolling-context.trigger_tokens', value: 150_000 }])
    expect(text).toMatch(/150,000 tokens/)
  })

  test('retention takes only its listed choices, case aside', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on)
    const bad = await $.command.run({ command: 'roll', args: 'set retention 2 weeks' })
    expect(bad.text).toMatch(/one of: 1 day, 14 days, Never/)
    await $.command.run({ command: 'roll', args: 'set retention never' })
    expect(sets).toEqual([{ key: 'rolling-context.retention', value: 'Never' }])
  })
})

// The panes the engine has open, answered beneath the mod; `placed` false is a
// surface that cannot place one.
function panes(on: On, placed = true) {
  const open = new Set<string>()
  on('ui.open', async ($, e) => {
    open.add(e.id)
    return { value: placed ? { isPlaced: true as const } : { isPlaced: false as const, reason: 'too narrow' } }
  })
  on('ui.close', async ($, e) => {
    open.delete(e.id)
    return { value: undefined }
  })
  on('ui.panes', async () => ({ value: [...open].map(id => ({ id, title: id })) }))
  return open
}

const HINT = { isDraft: false, isWorking: true, hint: '? for shortcuts' }

describe('the row under the prompt', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`shows the status, then the controls, then the hint (${surface})`, async ($, on) => {
      const w = world(on, 'Linux', 'rolling-context is now ON')
      config(on)
      panes(on)
      await start($, on)
      await $.command.run({ command: 'roll', args: 'status' }) // the first /health read
      const row = await $.ui.mount({ plugin: 'rolling-context', surface, component: 'PromptHint', props: HINT })
      const status = await row.find({ type: 'Text', text: /^Rolling context: / })
      expect(status?.text).toBe('Rolling context: off for this session · 4.7M tokens saved · 40 stored')
      expect(status?.props).toMatchObject({ bold: true, color: 'text' })
      expect((await row.find({ text: '? for shortcuts' }))).toBeDefined()
      // Drawn in the row, so not repeated on the status line.
      await $.command.run({ command: 'roll', args: 'status' })
      expect(w.statuses.at(-1)).toBeUndefined()

      // The world's /health says off for this session.
      expect((await row.find({ key: 'toggle' }))?.text).toMatch(/Turn on/)
      await row.press({ key: 'toggle' })
      expect(w.runs.at(-1)!.argv.slice(2)).toEqual(['on', '--session', SESSION])
    })
  }

  test('where the host draws no row under the prompt (the desktop app), the band carries it', async ($, on) => {
    const w = world(on, 'Linux', 'rolling-context is now ON')
    config(on)
    panes(on)
    await start($, on)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toMatch(/^off for this session/) // nothing drawn yet
    const band = await $.ui.mount({
      plugin: 'rolling-context', surface: 'desktop', component: 'AbovePrompt',
      props: {
        hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100,
        scroll: { offset: 0, bodyRows: 10 }, view: {},
      },
    })
    const status = await band.find({ type: 'Text', text: /^Rolling context: / })
    expect(status?.props).toMatchObject({ bold: true, color: 'text' })
    expect((await band.find({ key: 'toggle' }))?.text).toMatch(/Turn on/)
    await w.clock.advance(0)
    // Shown in the band, so the footer's status line no longer repeats it.
    expect(w.statuses.at(-1)).toBeUndefined()
    await band.press({ key: 'toggle' })
    expect(w.runs.at(-1)!.argv.slice(2)).toEqual(['on', '--session', SESSION])
  })

  test('Settings opens a pane with number fields, and closes it again', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on)
    const open = panes(on)
    await start($, on)
    const row = await $.ui.mount({ plugin: 'rolling-context', surface: 'desktop', component: 'PromptHint', props: HINT })
    await row.press({ key: 'settings' })
    expect(open.has('settings')).toBe(true)

    const pane = await $.ui.mount({
      plugin: 'rolling-context', surface: 'desktop', component: 'Pane', requestId: 'settings',
      props: { title: 'Rolling context settings', bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    } as Parameters<typeof $.ui.mount>[0])
    expect((await pane.find({ key: 'trigger' }))?.type).toBe('Input')
    expect((await pane.find({ key: 'trigger' }))?.props.value).toBe('200000')
    await pane.input({ key: 'trigger', text: '250000' })
    await pane.input({ key: 'target', text: '120k' })
    await pane.select({ key: 'retention', value: 'Never' })
    expect(sets).toEqual([
      { key: 'rolling-context.trigger_tokens', value: 250_000 },
      { key: 'rolling-context.target_tokens', value: 120_000 },
      { key: 'rolling-context.retention', value: 'Never' },
    ])

    await row.press({ key: 'settings' })
    expect(open.has('settings')).toBe(false)
  })

  test('where no pane can be placed, Settings toggles the box above the prompt', async ($, on) => {
    world(on, 'Linux', '')
    config(on)
    const open = panes(on, false)
    on('ui.render', async ($, e) => {   // the engine's own band: empty
      const { Box } = $.ui.resolve(e)
      return h(Box, {})
    })
    await start($, on)
    const row = await $.ui.mount({ plugin: 'rolling-context', surface: 'terminal', component: 'PromptHint', props: HINT })
    const band = await $.ui.mount({
      plugin: 'rolling-context', surface: 'terminal', component: 'AbovePrompt',
      props: {
        hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100,
        scroll: { offset: 0, bodyRows: 10 }, view: {},
      },
    })
    expect(await band.find({ key: 'trigger' })).toBeUndefined()
    await row.press({ key: 'settings' })
    expect(open.size).toBe(0) // the unplaced pane is not left waiting
    expect((await band.find({ key: 'trigger' }))?.type).toBe('Input')
    expect(await band.find({ key: 'toggle' })).toBeUndefined() // settings only
    await row.press({ key: 'settings' })
    expect(await band.find({ key: 'trigger' })).toBeUndefined()
  })
})
