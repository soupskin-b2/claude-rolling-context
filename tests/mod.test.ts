// Tests for hooks/register.tsx, run by `claude plugin test`.
// Nothing here touches a real process, the network or the disk: the test's
// hooks answer $.process.run, $.http.fetch and $.session.id beneath the mod.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1234'

type Run = { argv: readonly string[]; env: Record<string, string> }

// hangFetch: /health never answers, as a fetch through a proxy that cannot
// relay it did. tokens: the context tokens $.session.usage reports, or null
// for an engine that reports none. session: /health's answer for this
// session (a proxy before 1.14.9 counts no compressions there).
const SESSION_HEALTH = { enabled: false, scope: 'this session', compressions: 3, tokens_saved: 393_580 }
function world(
  on: On, os: 'Windows_NT' | 'Linux', out: string | { stdout: string; exitCode: number }, hangFetch = false,
  tokens: number | null = 302_500, session: object = SESSION_HEALTH, active = 0,
) {
  const runs: Run[] = []
  const statuses: (string | undefined)[] = []
  mock.env(on, os === 'Windows_NT'
    ? { OS: 'Windows_NT', USERPROFILE: 'C:/Users/u' }
    : { HOME: '/home/u' })
  const clock = mock.clock(on)
  on('session.id', async () => ({ value: SESSION }))
  on('session.usage', async () => tokens === null ? ({ value: undefined }) : ({
    value: {
      startedAt: 0,
      context: { tokens, window: 200_000, percent: 0 },
      rateLimits: [],
      cost: {},
    },
  }))
  on('process.run', async ($, e) => {
    runs.push({ argv: e.argv, env: { ...(e.init?.env ?? {}) } })
    const { stdout, exitCode } = typeof out === 'string' ? { stdout: out, exitCode: 0 } : out
    return { value: { exitCode, stdout, stderr: '' } }
  })
  on('http.fetch', async () => hangFetch ? new Promise<never>(() => {}) : ({
    value: {
      status: 200, ok: true, headers: {},
      text: JSON.stringify({
        service: 'rolling-context', enabled: true, total_tokens_saved: 4_700_000,
        stored_compressions: 40, active_compressions: active,
        session,
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
    expect(w.statuses.at(-1)).toBe('off for this session · 302.5k context · 3 compressions, 394k tokens saved, 40 stored')
  })
})

describe('registration', () => {
  test('/roll is the one command the plugin registers', async ($, on) => {
    world(on, 'Linux', '')
    const names: string[] = []
    on('command.register', async ($, e) => {
      names.push(e.name)
      return { value: { command: e.name } }
    })
    on('ui.log', async () => ({ value: undefined }))
    on('session.start', async ($, e) => e)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    expect(names).toEqual(['roll'])
  })

  test('if /roll is refused (a taken name), the log says so', async ($, on) => {
    world(on, 'Linux', '')
    const logs: unknown[] = []
    on('command.register', async () => ({ deny: '"/roll" refused: it is taken' }))
    on('ui.log', async ($, e) => {
      logs.push(e)
      return { value: undefined }
    })
    on('session.start', async ($, e) => e)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    expect(JSON.stringify(logs)).toMatch(/\/roll is unavailable/)
  })
})

// The plugin's /config rows, answered beneath the mod; what it set is kept.
// `id` is the provider's plugin id and `prefix` the keys' first part. The
// engine (2.1.295, the desktop app) answers `rolling-context@inline` as the
// provider and `rolling-context.<field>` as the key; the default keys by the
// provider id, the shape the tests were first written against.
function config(on: On, id = 'rolling-context@suite', prefix = id) {
  const values: Record<string, number | string> = {
    [`${prefix}.trigger_tokens`]: 200_000,
    [`${prefix}.target_tokens`]: 100_000,
    [`${prefix}.retention`]: '14 days',
  }
  const sets: { key: string; value: unknown }[] = []
  on('config.list', async () => ({
    value: Object.entries(values).map(([key, value]) => ({
      key, label: key, value,
      kind: typeof value === 'number' ? ('number' as const) : ('choice' as const),
      options: key.endsWith('.retention') ? ['1 day', '14 days', 'Never'] : undefined,
      provider: { plugin: id, tier: 'user' as const },
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
    expect(sets).toEqual([{ key: 'rolling-context@suite.trigger_tokens', value: 150_000 }])
    expect(text).toMatch(/150,000 tokens/)
  })

  test('retention takes only its listed choices, case aside', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on)
    const bad = await $.command.run({ command: 'roll', args: 'set retention 2 weeks' })
    expect(bad.text).toMatch(/one of: 1 day, 14 days, Never/)
    await $.command.run({ command: 'roll', args: 'set retention never' })
    expect(sets).toEqual([{ key: 'rolling-context@suite.retention', value: 'Never' }])
  })

  test('finds the rows when the provider names the bare plugin', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on, 'rolling-context')
    const { text } = await $.command.run({ command: 'roll', args: 'set trigger 150k' })
    expect(sets).toEqual([{ key: 'rolling-context.trigger_tokens', value: 150_000 }])
    expect(text).toMatch(/150,000 tokens/)
  })

  test('finds the rows the engine answers: inline provider, bare-name keys', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on, 'rolling-context@inline', 'rolling-context')
    const { text } = await $.command.run({ command: 'roll', args: 'set retention 1 day' })
    expect(sets).toEqual([{ key: 'rolling-context.retention', value: '1 day' }])
    expect(text).toMatch(/retention set to 1 day/)
  })

  test('records the rows\' pluginConfigs id for the proxy to read', async ($, on) => {
    world(on, 'Linux', '')
    config(on, 'rolling-context@inline', 'rolling-context')
    const files: Record<string, string> = {}
    on('fs.read', async ($, e) =>
      e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' })
    on('fs.write', async ($, e) => {
      files[e.path] = e.text
      return { value: undefined }
    })
    await $.command.run({ command: 'roll', args: 'set retention 1 day' })
    const [path, text] = Object.entries(files)[0] ?? []
    // The engine hands the write a native path.
    expect(path?.replace(/\\/g, '/')).toMatch(/\/home\/u\/\.claude\/rolling-context-config-id$/)
    expect(text).toBe('rolling-context@inline\n')
  })

  test('finds the rows by key when the provider is spelled some other way', async ($, on) => {
    world(on, 'Linux', '')
    const sets = config(on, 'rc-renamed', 'rolling-context')
    await $.command.run({ command: 'roll', args: 'set target 50k' })
    expect(sets).toEqual([{ key: 'rolling-context.target_tokens', value: 50_000 }])
  })

  // A desktop session can start with none of the plugin's rows (the engine
  // left it out of the plugins it builds them from): switch.py writes it.
  test('with no row, switch.py writes the option into settings.json', async ($, on) => {
    const w = world(on, 'Linux', 'trigger_tokens = 150000')
    config(on, 'other-plugin')
    const { text } = await $.command.run({ command: 'roll', args: 'set trigger 150k' })
    expect(w.runs.at(-1)!.argv.slice(1)).toEqual([
      expect.stringMatching(/proxy\/switch\.py$/), 'set', 'trigger_tokens', '150000'])
    expect(text).toMatch(/^trigger set to 150,000 tokens in settings\.json/)
  })

  test('a retention choice is checked before it is written, without a row too', async ($, on) => {
    const w = world(on, 'Linux', '')
    config(on, 'other-plugin')
    const { text } = await $.command.run({ command: 'roll', args: 'set retention 2 weeks' })
    expect(text).toMatch(/one of: 1 hour, 6 hours/)
    expect(w.runs.length).toBe(0)
  })

  test('when that write fails too, says why, what /config holds and where the plugin loaded from', async ($, on) => {
    world(on, 'Linux', { stdout: 'settings.json could not be written (EACCES); not changed', exitCode: 1 })
    config(on, 'other-plugin')
    const { text } = await $.command.run({ command: 'roll', args: 'set trigger 150k' })
    expect(text).toMatch(/^Not changed: settings\.json could not be written/)
    expect(text).toMatch(/No \/config row for trigger/)
    expect(text).toMatch(/other-plugin\.trigger_tokens \(other-plugin\)/)
    expect(text).toMatch(/plugin loaded from /)
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
      expect(status?.text).toBe('Rolling context: off for this session · 302.5k context · 3 compressions, 394k tokens saved, 40 stored')
      expect(status?.props).toMatchObject({ bold: true, color: 'text' })
      expect((await row.find({ text: '? for shortcuts' }))).toBeDefined()
      // The status line keeps the status too: the row holds the buttons, the
      // line holds the words.
      await $.command.run({ command: 'roll', args: 'status' })
      expect(w.statuses.at(-1)).toBe('off for this session · 302.5k context · 3 compressions, 394k tokens saved, 40 stored')

      // The world's /health says off for this session.
      expect((await row.find({ key: 'toggle' }))?.text).toMatch(/Turn on/)
      expect((await row.find({ key: 'restart' }))?.text).toBe('Restart')
      expect((await row.find({ key: 'settings' }))?.text).toMatch(/Settings/)
      await row.press({ key: 'toggle' })
      expect(w.runs.at(-1)!.argv.slice(2)).toEqual(['on', '--session', SESSION])
    })
  }

  test('where the host draws no row under the prompt (the desktop app), the band carries one compact line', async ($, on) => {
    const w = world(on, 'Linux', 'rolling-context is now ON')
    config(on)
    panes(on)
    on('ui.render', async ($, e) => {   // the engine's own band: empty
      const { Box } = $.ui.resolve(e)
      return h(Box, {})
    })
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
    // One dim "RC", then Toggle/Restart/Settings: the status itself lives in
    // the status line, so the band costs a single line.
    const rc = await band.find({ type: 'Text', text: /^RC$/ })
    expect(rc?.props).toMatchObject({ dimColor: true })
    expect(await band.find({ type: 'Text', text: /^Rolling context: / })).toBeUndefined()
    expect((await band.find({ key: 'toggle' }))?.text).toMatch(/Turn on/)
    expect((await band.find({ key: 'restart' }))?.text).toBe('Restart')
    expect((await band.find({ key: 'settings' }))?.text).toMatch(/Settings/)
    // The status line keeps the status: the band holds the buttons, the line
    // holds the words.
    expect(w.statuses.at(-1)).toMatch(/^off for this session/)
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
    expect((await row.find({ key: 'settings' }))?.text).toBe('Hide settings')

    const pane = await $.ui.mount({
      plugin: 'rolling-context', surface: 'desktop', component: 'Pane', requestId: 'settings',
      props: { title: 'Rolling context settings', bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    } as Parameters<typeof $.ui.mount>[0])
    expect((await pane.find({ key: 'trigger' }))?.type).toBe('Input')
    expect((await pane.find({ key: 'trigger' }))?.props.value).toBe('200000')
    expect((await pane.find({ key: 'trigger' }))?.props.label).toBe('Compact trigger (tokens) ')
    await pane.input({ key: 'trigger', text: '250000' })
    await pane.input({ key: 'target', text: '120k' })
    await pane.select({ key: 'retention', value: 'Never' })
    expect(sets).toEqual([
      { key: 'rolling-context@suite.trigger_tokens', value: 250_000 },
      { key: 'rolling-context@suite.target_tokens', value: 120_000 },
      { key: 'rolling-context@suite.retention', value: 'Never' },
    ])

    await row.press({ key: 'settings' })
    expect(open.has('settings')).toBe(false)
  })

  test('closing Settings with the button says Settings again', async ($, on) => {
    world(on, 'Linux', '')
    config(on)
    const open = panes(on)
    await start($, on)
    const row = await $.ui.mount({ plugin: 'rolling-context', surface: 'desktop', component: 'PromptHint', props: HINT })
    await row.press({ key: 'settings' })
    expect((await row.find({ key: 'settings' }))?.text).toBe('Hide settings')
    await row.press({ key: 'settings' })
    expect(open.has('settings')).toBe(false)
    // The close writes no atom, so the row relies on the invalidate the
    // button requests; redraw to the same steady state and read it.
    await row.redraw()
    expect((await row.find({ key: 'settings' }))?.text).toBe('Settings')
  })

  test('where the /config row carries no options, retention still offers every choice', async ($, on) => {
    world(on, 'Linux', '')
    on('config.list', async () => ({
      value: [
        { key: 'rolling-context@suite.trigger_tokens', label: 'trigger', value: 200_000, kind: 'number' as const, provider: { plugin: 'rolling-context@suite', tier: 'user' as const }, isLocked: false },
        { key: 'rolling-context@suite.target_tokens', label: 'target', value: 100_000, kind: 'number' as const, provider: { plugin: 'rolling-context@suite', tier: 'user' as const }, isLocked: false },
        { key: 'rolling-context@suite.retention', label: 'retention', value: '14 days', kind: 'choice' as const, provider: { plugin: 'rolling-context@suite', tier: 'user' as const }, isLocked: false },
      ],
    }))
    on('config.set', async ($, e) => ({ value: e.value }))
    panes(on)
    await start($, on)
    const row = await $.ui.mount({ plugin: 'rolling-context', surface: 'desktop', component: 'PromptHint', props: HINT })
    await row.press({ key: 'settings' })
    const pane = await $.ui.mount({
      plugin: 'rolling-context', surface: 'desktop', component: 'Pane', requestId: 'settings',
      props: { title: 'Rolling context settings', bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    } as Parameters<typeof $.ui.mount>[0])
    const options = (await pane.find({ key: 'retention' }))?.props.options as { value: string }[]
    expect(options.map(o => o.value)).toEqual([
      '1 hour', '6 hours', '1 day', '3 days', '7 days', '14 days', '30 days', 'Never',
    ])
    await pane.select({ key: 'retention', value: 'Never' }) // accepted, not "takes one of"
  })

  test('with no rows, the pane shows what settings.json holds, the recorded entry first', async ($, on) => {
    world(on, 'Linux', '')
    config(on, 'other-plugin')
    const files: Record<string, string> = {
      '/home/u/.claude/rolling-context-config-id': 'rolling-context@inline\n',
      '/home/u/.claude/settings.json': JSON.stringify({
        pluginConfigs: {
          'rolling-context@soupskin-plugins': { options: { trigger_tokens: 200_000 } },
          'rolling-context@inline': { options: { trigger_tokens: 180_000, retention: '3 days' } },
          'other-plugin': { options: { trigger_tokens: 1 } },
        },
      }),
    }
    on('fs.read', async ($, e) => {
      // The engine hands the read a native path.
      const path = Object.keys(files).find(f => e.path.replace(/\\/g, '/').endsWith(f))
      return path ? { value: files[path] } : { deny: 'ENOENT' }
    })
    panes(on)
    await start($, on)
    const row = await $.ui.mount({ plugin: 'rolling-context', surface: 'desktop', component: 'PromptHint', props: HINT })
    await row.press({ key: 'settings' })
    const pane = await $.ui.mount({
      plugin: 'rolling-context', surface: 'desktop', component: 'Pane', requestId: 'settings',
      props: { title: 'Rolling context settings', bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    } as Parameters<typeof $.ui.mount>[0])
    expect((await pane.find({ key: 'trigger' }))?.props.value).toBe('180000')
    expect((await pane.find({ key: 'target' }))?.props.value).toBe('40000') // the default
    expect((await pane.find({ key: 'retention' }))?.props.value).toBe('3 days')
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
    expect(await band.find({ key: 'toggle' })).toBeUndefined() // the row below holds the buttons here
    expect(await band.find({ type: 'Text', text: /^RC$/ })).toBeUndefined()
    await row.press({ key: 'settings' })
    expect(open.size).toBe(0) // the unplaced pane is not left waiting
    expect((await band.find({ key: 'trigger' }))?.type).toBe('Input')
    expect(await band.find({ key: 'toggle' })).toBeUndefined() // settings only
    await row.press({ key: 'settings' })
    expect(await band.find({ key: 'trigger' })).toBeUndefined()
  })
})

describe('context token count', () => {
  test('formats thousands with one decimal (302.5k)', async ($, on) => {
    const w = world(on, 'Linux', 'ok')
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('off for this session · 302.5k context · 3 compressions, 394k tokens saved, 40 stored')
  })

  test('trims exact thousands (40k, not 40.0k)', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 40_000)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('off for this session · 40k context · 3 compressions, 394k tokens saved, 40 stored')
  })

  test('shows small counts plainly (999)', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 999)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('off for this session · 999 context · 3 compressions, 394k tokens saved, 40 stored')
  })

  test('formats millions with one decimal (2.5M)', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 2_500_000)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('off for this session · 2.5M context · 3 compressions, 394k tokens saved, 40 stored')
  })

  test('omits the segment when usage is unavailable', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, null)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('off for this session · 3 compressions, 394k tokens saved, 40 stored')
  })
})

describe('the session\'s own compressions', () => {
  test('one compression reads singular', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 40_000,
      { enabled: true, scope: 'default', compressions: 1, tokens_saved: 120_000 })
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('on · 40k context · 1 compression, 120k tokens saved, 40 stored')
  })

  test('a proxy that counts no session\'s share shows only what it stores', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 40_000, { enabled: true, scope: 'default' })
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('on · 40k context · 40 stored')
  })

  test('while compressing, that replaces the state and context', async ($, on) => {
    const w = world(on, 'Linux', 'ok', false, 40_000, SESSION_HEALTH, 1)
    await $.command.run({ command: 'roll', args: 'status' })
    expect(w.statuses.at(-1)).toBe('compressing… · 3 compressions, 394k tokens saved, 40 stored')
  })
})
