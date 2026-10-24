import type { EngineInterface, Register } from 'claude-code'

// Every tool runs worker/api_worker.py with the Agent SDK's Python. The key,
// budgets and task files are the worker's; this module only relays.
const DISPATCH_DESCRIPTION = `Hand a self-contained job to a background Claude Agent SDK worker that is billed to the user's Console API key (their plan's monthly API credits), not to this Claude Code subscription. Returns a task id at once; the worker keeps running after this call.

Use it when the user asks to offload work to the API worker, or for long, independent jobs (a refactor on its own branch, writing tests, a research write-up) the user agreed to send there. The worker cannot ask questions and has no memory of this conversation: put every needed fact, file path and acceptance check in the prompt.

In a git repository the worker gets its own worktree on branch api-worker/<id>, so it never touches the files here; review and merge that branch afterwards. Writes are limited to its directory. Bash and web access are off unless allow_bash / allow_web are set. Each task stops at budget_usd (default $2), and a monthly cap refuses tasks that would exceed it.`

const TOOLS = [
  {
    name: 'dispatch',
    description: DISPATCH_DESCRIPTION,
    inputSchema: {
      type: 'object',
      properties: {
        prompt: { type: 'string', description: 'The complete, self-contained task for the worker.' },
        title: { type: 'string', description: 'Short label for the task list.' },
        cwd: { type: 'string', description: 'Directory to work in (absolute). Default: this session\'s directory.' },
        budget_usd: { type: 'number', description: 'Most this task may spend, in US dollars. Default 2.' },
        key: { type: 'string', description: 'Which stored API key (see list.keys). Default: the one with the most left this month.' },
        model: { type: 'string', description: 'Model id. Default from the worker config.' },
        allow_bash: { type: 'boolean', description: 'Let the worker run shell commands (tests, builds).' },
        allow_web: { type: 'boolean', description: 'Let the worker use WebFetch and WebSearch.' },
        worktree: { type: 'boolean', description: 'Work in a separate git worktree and branch (default true).' },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'cloud',
    description: `Start a Claude Code cloud session (claude.ai/code) for a self-contained job. It is paid from the user's cloud session credits while they last (then the plan's normal usage), not from this session's usage and not from the API key. Returns the session URL within about a minute, plus warnings about uncommitted or unpushed work, and records it in the api-worker task list. Tell the user the URL and any warnings.

Where cwd is not a git repository with a remote, the task runs in the user's private cloud-desk repository: pass the files it needs in files; they are copied to tasks/<id>/input/ there, the session writes tasks/<id>/output/, and collect copies those outputs back here once done.

In a git repository with a remote, the session is told to push its work and a report to branch claude/aw-<id>; status shows "done" once that branch appears, and result fetches it (without touching the working tree) and returns the report, commits and diff stat, ready to review and merge.

The cloud session clones the repository from its git remote: it sees only what is pushed, not local uncommitted files, and works on its own branch there. Use it when the user asks for a cloud session or to spend the cloud credits; prefer dispatch (API key) for work on local, unpushed files. The cloud session cannot ask questions: put every needed fact and acceptance check in the prompt.`,
    inputSchema: {
      type: 'object',
      properties: {
        prompt: { type: 'string', description: 'The complete, self-contained task for the cloud session.' },
        title: { type: 'string', description: 'Short label (3-5 words).' },
        cwd: { type: 'string', description: 'Repository directory (absolute). Default: this session\'s directory.' },
        files: {
          type: 'array',
          items: { type: 'string' },
          description: 'Files or folders (relative to cwd, or absolute) to upload when the folder is not a git repository with a remote. They go to the user\'s private cloud-desk repository; files that look like secrets are refused. Name only what the task needs.',
        },
        desk: { type: 'boolean', description: 'Use the cloud-desk repository even if cwd is its own git repository.' },
        account: { type: 'string', description: 'cswap account number or email whose cloud credits to use (see list.accounts). Default: the active account. Another account means a brief live-login switch (a few seconds) that all running Claude Code sessions share; only pass it when the user asks for that account.' },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'collect',
    description: 'Copy the output files of a finished cloud-desk task into a local folder (default <its folder>/cloud-results/<id>/; never overwrites).',
    inputSchema: {
      type: 'object',
      properties: { id: { type: 'string' }, to: { type: 'string', description: 'Destination folder (absolute), must be empty or new.' } },
      required: ['id'],
    },
  },
  {
    name: 'status',
    description: 'State of one API worker task (queued, running, done, failed, cancelled, lost), its cost so far and its latest steps.',
    inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
  },
  {
    name: 'result',
    description: 'The final report of a finished task: for an API task its report, cost and branch; for a cloud task the pulled-back report, commits, diff stat and merge command.',
    inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
  },
  {
    name: 'list',
    description: 'Recent tasks (API and cloud; final is true once a task has ended), each API key\'s spend this month and what is left, and the cswap accounts usable for cloud sessions.',
    inputSchema: { type: 'object', properties: { limit: { type: 'number' } } },
  },
  {
    name: 'cancel',
    description: 'Stop a running API worker task.',
    inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
  },
] as const

// cloud can stage files (git push, up to 3 min), switch accounts and wait for
// the session link; collect fetches a branch. Everything else is quick.
const TIMEOUT_MS = 120_000
const LONG_TIMEOUT_MS = 400_000

async function worker($: EngineInterface, args: string[], stdin?: string, timeoutMs = TIMEOUT_MS) {
  const home = (await $.env.get('HOME')) ?? ''
  const python = (await $.env.get('API_WORKER_PYTHON')) || `${home}/.local/share/api-worker/venv/bin/python`
  const r = await $.process.run([python, `${$.plugin.root}/worker/api_worker.py`, ...args], {
    stdin,
    timeoutMs,
  })
  if (r.exitCode !== 0) {
    let message = r.stderr.trim() || `api-worker exited with ${r.exitCode}`
    try {
      message = JSON.parse(message).error ?? message
    } catch {}
    if (/ENOENT|No such file/.test(message) && !r.stdout) {
      message = `${message}\nThe worker's Python is missing: run install.sh from the api-worker folder.`
    }
    throw new Error(message)
  }
  return JSON.parse(r.stdout)
}

export const register: Register = on => {
  // Tasks this session dispatched and has not yet announced as finished.
  const watching = new Set<string>()
  let poll: { cancel: () => void } | undefined

  on('session.start', async ($, e, next) => {
    for (const t of TOOLS) await $.tool.register({ ...t, isDeferred: false })
    poll?.cancel()
    poll = $.clock.every(30_000, () => {
      if (watching.size === 0) return
      worker($, ['list', '--limit', '50', '--no-accounts'])
        .then(({ tasks }) => {
          for (const t of tasks) {
            if (!watching.has(t.id) || t.final !== true) continue
            watching.delete(t.id)
            const cost = typeof t.costUsd === 'number' ? ` ($${t.costUsd.toFixed(2)})` : ''
            const kind = t.kind === 'cloud' ? 'Cloud task' : 'API worker'
            $.ui.toast(`${kind} ${t.id} ${t.state}${cost}: ${t.title}`, { timeoutMs: 10_000 })
          }
        })
        .catch(() => {})
    })
    return next(e)
  })

  for (const t of TOOLS) {
    on('tool.call', { tool: `mcp__api-worker__${t.name}` }, async ($, e) => {
      const input = e as Record<string, unknown>
      const id = typeof input.id === 'string' ? input.id : ''
      try {
        switch (t.name) {
          case 'dispatch': {
            const args = ['start', '--cwd', String(input.cwd || (await $.session.cwd()) || '.')]
            if (input.title) args.push('--title', String(input.title))
            if (input.model) args.push('--model', String(input.model))
            if (typeof input.budget_usd === 'number') args.push('--budget', String(input.budget_usd))
            if (input.key) args.push('--key', String(input.key))
            if (input.allow_bash === true) args.push('--allow-bash')
            if (input.allow_web === true) args.push('--allow-web')
            if (input.worktree === false) args.push('--no-worktree')
            const started = await worker($, args, String(input.prompt ?? ''))
            watching.add(started.id)
            $.ui.toast(`API worker ${started.id} started: ${started.title}`)
            return { result: JSON.stringify(started, null, 2) }
          }
          case 'cloud': {
            const args = ['cloud', '--cwd', String(input.cwd || (await $.session.cwd()) || '.')]
            if (input.title) args.push('--title', String(input.title))
            if (input.account) args.push('--account', String(input.account))
            if (input.desk === true) args.push('--desk')
            if (Array.isArray(input.files)) for (const f of input.files) args.push('--file', String(f))
            const started = await worker($, args, String(input.prompt ?? ''), LONG_TIMEOUT_MS)
            if (started.resultBranch) watching.add(started.id)
            $.ui.toast(`Cloud session started: ${started.title}`)
            return { result: JSON.stringify(started, null, 2) }
          }
          case 'collect': {
            const args = ['collect', id]
            if (input.to) args.push('--to', String(input.to))
            return { result: JSON.stringify(await worker($, args, undefined, LONG_TIMEOUT_MS), null, 2) }
          }
          case 'status':
            return { result: JSON.stringify(await worker($, ['status', id]), null, 2) }
          case 'result':
            return { result: JSON.stringify(await worker($, ['result', id]), null, 2) }
          case 'cancel':
            watching.delete(id)
            return { result: JSON.stringify(await worker($, ['cancel', id]), null, 2) }
          case 'list': {
            const limit = typeof input.limit === 'number' ? Math.max(1, Math.floor(input.limit)) : 20
            return { result: JSON.stringify(await worker($, ['list', '--limit', String(limit)]), null, 2) }
          }
        }
      } catch (err) {
        return { deny: `api-worker: ${err instanceof Error ? err.message : String(err)}` }
      }
      return { deny: 'api-worker: unknown tool' }
    }).catch(() => ({ deny: 'api-worker: the plugin failed; try again or run api-worker from a terminal.' }))
  }
}
