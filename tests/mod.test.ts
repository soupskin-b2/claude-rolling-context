// Tests for hooks/register.ts, run by `claude plugin test`.
// Nothing here touches a real process, the network or the disk: the test's
// hooks answer $.process.run, $.http.fetch and $.session.id beneath the mod.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = 'sess-1234'

type Run = { argv: readonly string[]; env: Record<string, string> }

function world(on: On, os: 'Windows_NT' | 'Linux', stdout: string) {
  const runs: Run[] = []
  const statuses: (string | undefined)[] = []
  mock.env(on, os === 'Windows_NT'
    ? { OS: 'Windows_NT', USERPROFILE: 'C:/Users/u' }
    : { HOME: '/home/u' })
  mock.clock(on)
  on('session.id', async () => ({ value: SESSION }))
  on('process.run', async ($, e) => {
    runs.push({ argv: e.argv, env: { ...(e.init?.env ?? {}) } })
    return { value: { exitCode: 0, stdout, stderr: '' } }
  })
  on('http.fetch', async () => ({
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
  return { runs, statuses }
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
