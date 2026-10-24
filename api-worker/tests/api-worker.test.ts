import { expect, mock, test } from 'claude-code/testing'

const start = async ($: any, on: any) => {
  on('tool.register', ($: unknown, e: { name: string }) => ({ value: { tool: `mcp__api-worker__${e.name}` } }))
  on('session.start', ($: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/repo', surface: null, isInteractive: true })
}

const ok = (stdout: unknown) => ({
  value: { exitCode: 0, stdout: JSON.stringify(stdout), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

test('dispatch runs the worker with the prompt on stdin and the session directory', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    return ok({ id: 't0123abcd', title: 'Write tests', workdir: '/w', branch: 'api-worker/t0123abcd' })
  })
  on('session.cwd', () => ({ value: '/repo' }))
  await start($, on)

  const r = await $.tool.call({
    tool: 'mcp__api-worker__dispatch',
    tool_use_id: 'u1',
    prompt: 'Write tests for x',
    budget_usd: 3,
    key: 'b',
    allow_bash: true,
  } as never)

  expect(JSON.parse((r as { result: string }).result).id).toBe('t0123abcd')
  expect(runs.length).toBe(1)
  const argv = runs[0]!.argv
  expect(argv[0]).toBe('/home/u/.local/share/api-worker/venv/bin/python')
  expect(argv[1]!.endsWith('/worker/api_worker.py')).toBe(true)
  expect(argv.slice(2)).toEqual(['start', '--cwd', '/repo', '--budget', '3', '--key', 'b', '--allow-bash'])
  expect(runs[0]!.stdin).toBe('Write tests for x')
})

test('a worker error reaches the model as a deny with its message', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  on('process.run', () => ({
    value: { exitCode: 1, stdout: '', stderr: JSON.stringify({ error: '本月已用 $199' }), isStdoutTruncated: false, isStderrTruncated: false },
  }))
  await start($, on)

  const r = await $.tool.call({ tool: 'mcp__api-worker__status', tool_use_id: 'u2', id: 't00000000' } as never)
  expect(JSON.stringify(r)).toContain('本月已用 $199')
})

test('a finished task this session dispatched raises a toast', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const clock = mock.clock(on)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push((e as { text: string }).text)
    return { value: undefined } as never
  })
  let state = 'running'
  const lists: (readonly string[])[] = []
  on('process.run', ($, e) => {
    if (e.argv[2] === 'start') return ok({ id: 't11111111', title: 'Job' })
    lists.push(e.argv.slice(2))
    return ok({ tasks: [{ id: 't11111111', title: 'Job', state, final: state === 'done', costUsd: 0.5 }] })
  })
  await start($, on)
  await $.tool.call({ tool: 'mcp__api-worker__dispatch', tool_use_id: 'u3', prompt: 'p', cwd: '/w' } as never)

  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('done'))).toBe(false)
  state = 'done'
  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('t11111111 done ($0.50)'))).toBe(true)
  const count = toasts.length
  await clock.advance(30_000)
  expect(toasts.length).toBe(count)
  expect(lists[0]).toEqual(['list', '--limit', '50', '--no-accounts'])
})

test('cloud runs the worker cloud command with the prompt on stdin', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    return ok({ id: 't22222222', title: 'tests', sessionUrl: 'https://claude.ai/code/session_abc', warnings: [] })
  })
  on('session.cwd', () => ({ value: '/repo' }))
  await start($, on)

  const r = await $.tool.call({ tool: 'mcp__api-worker__cloud', tool_use_id: 'u4', prompt: 'Add tests', title: 'tests', account: '3', files: ['a.csv', 'docs'] } as never)
  expect(runs[0]!.argv.slice(2)).toEqual(['cloud', '--cwd', '/repo', '--title', 'tests', '--account', '3', '--file', 'a.csv', '--file', 'docs'])
  expect(runs[0]!.stdin).toBe('Add tests')
  expect(JSON.parse((r as { result: string }).result).sessionUrl).toBe('https://claude.ai/code/session_abc')
})

test('a cloud task whose branch came back raises a toast', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const clock = mock.clock(on)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push((e as { text: string }).text)
    return { value: undefined } as never
  })
  let state = 'running'
  on('process.run', ($, e) =>
    e.argv[2] === 'cloud'
      ? ok({ id: 't33333333', title: 'Cloud job', resultBranch: 'claude/aw-t33333333', sessionUrl: 'https://claude.ai/code/s' })
      : ok({ tasks: [{ id: 't33333333', kind: 'cloud', title: 'Cloud job', state, final: state === 'done' }] }),
  )
  await start($, on)
  await $.tool.call({ tool: 'mcp__api-worker__cloud', tool_use_id: 'u6', prompt: 'p', cwd: '/w' } as never)
  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('done'))).toBe(false)
  state = 'done'
  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('Cloud task t33333333 done'))).toBe(true)
})

test('collect copies a desk task\'s outputs', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const runs: (readonly string[])[] = []
  on('process.run', ($, e) => {
    runs.push(e.argv)
    return ok({ id: 't44444444', to: '/out', files: ['summary.md'] })
  })
  await start($, on)
  const r = await $.tool.call({ tool: 'mcp__api-worker__collect', tool_use_id: 'u7', id: 't44444444', to: '/out' } as never)
  expect(runs[0]!.slice(2)).toEqual(['collect', 't44444444', '--to', '/out'])
  expect(JSON.parse((r as { result: string }).result).files).toEqual(['summary.md'])
})

test('cloud and collect get a longer timeout than the quick calls', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const timeouts: Record<string, number | undefined> = {}
  on('process.run', ($, e) => {
    timeouts[e.argv[2]!] = e.init?.timeoutMs
    return ok({ id: 't55555555', title: 't', sessionUrl: 'https://claude.ai/code/s', files: [], tasks: [] })
  })
  await start($, on)
  await $.tool.call({ tool: 'mcp__api-worker__cloud', tool_use_id: 'u8', prompt: 'p', cwd: '/w' } as never)
  await $.tool.call({ tool: 'mcp__api-worker__collect', tool_use_id: 'u9', id: 't55555555' } as never)
  await $.tool.call({ tool: 'mcp__api-worker__status', tool_use_id: 'u10', id: 't55555555' } as never)
  await $.tool.call({ tool: 'mcp__api-worker__list', tool_use_id: 'u11' } as never)
  expect(timeouts).toEqual({ cloud: 400_000, collect: 400_000, status: 120_000, list: 120_000 })
})

test('a task is announced only when the worker marks it final', async ($, on) => {
  mock.env(on, { HOME: '/home/u' })
  const clock = mock.clock(on)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push((e as { text: string }).text)
    return { value: undefined } as never
  })
  let task: Record<string, unknown> = { id: 't66666666', title: 'Job', state: 'something-new', final: false }
  on('process.run', ($, e) => (e.argv[2] === 'start' ? ok({ id: 't66666666', title: 'Job' }) : ok({ tasks: [task] })))
  await start($, on)
  await $.tool.call({ tool: 'mcp__api-worker__dispatch', tool_use_id: 'u12', prompt: 'p', cwd: '/w' } as never)
  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('t66666666 something'))).toBe(false)
  task = { ...task, state: 'another-end-state', final: true }
  await clock.advance(30_000)
  expect(toasts.some(t => t.includes('t66666666 another-end-state'))).toBe(true)
})
