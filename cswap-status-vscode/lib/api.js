// api-worker data: month spend per Console API key and its tasks (API workers
// and cloud sessions). Everything comes from `api-worker list` / `status` /
// `result` JSON; the key itself is never read or shown here.

const vscode = require("vscode");
const { cfg } = require("./config");
const { resolveBin, exec } = require("./proc");
const { isFinal, taskStateZh, money } = require("./format");

const state = {
  data: null, // last good `api-worker list` output
  error: "", // the last refresh failed (shown even when data is kept)
  states: null, // id -> final?, to notice tasks that just finished
  noAccountsFlag: true, // pass --no-accounts until the worker rejects it
};
const changed = new vscode.EventEmitter();

const bin = () => resolveBin("apiWorkerPath", "api-worker");
const available = () => !!bin();
const enabled = () => cfg("showApi", true);

// Runs api-worker; resolves {code, out, err} with a JSON {"error": ...} on
// stderr turned into its message.
async function run(args, opts) {
  const b = bin();
  if (!b) return { code: -1, out: "", err: "api-worker 未安装" };
  const o = opts || {};
  const r = await exec(b, args, { timeout: o.timeout || 30000, input: o.input });
  let msg = r.err;
  try {
    msg = JSON.parse(msg).error || msg;
  } catch (e) {
    /* plain text */
  }
  if (r.timedOut) msg = `api-worker ${args[0]} 超时`;
  return { code: r.code, out: r.out, err: r.code === 0 ? msg : msg || `api-worker ${args[0]} 失败` };
}

// Settings changed: the binary may be another one now.
function reset() {
  state.noAccountsFlag = true;
  state.states = null;
}

async function list() {
  const base = ["list", "--limit", "30"];
  if (state.noAccountsFlag) {
    const r = await run([...base, "--no-accounts"]);
    // an older worker rejects the flag: retry once without, and stop asking
    if (r.code !== 0 && /no-accounts|unrecognized arguments/.test(r.err)) {
      state.noAccountsFlag = false;
      return run(base);
    }
    return r;
  }
  return run(base);
}

// Returns the finished tasks (for the caller to announce).
async function refresh() {
  vscode.commands.executeCommand("setContext", "cswapStatus.apiAvailable", available());
  if (!enabled() || !available()) {
    state.data = null;
    state.error = "";
    changed.fire();
    return [];
  }
  const r = await list();
  if (r.code !== 0) {
    state.error = r.err;
    changed.fire();
    return [];
  }
  let data;
  try {
    data = JSON.parse(r.out);
  } catch (e) {
    state.error = "api-worker list 的输出无法解析";
    changed.fire();
    return [];
  }
  state.data = data;
  state.error = "";
  const tasks = data.tasks || [];
  // Tasks that finished since the last refresh (not on the first one).
  const finished = [];
  const now = new Map(tasks.map((t) => [t.id, isFinal(t)]));
  if (state.states) {
    for (const t of tasks) {
      if (state.states.get(t.id) === false && isFinal(t)) finished.push(t);
    }
  }
  state.states = now;
  changed.fire();
  return finished;
}

function taskLine(t) {
  const cost = typeof t.costUsd === "number" ? ` · ${money(t.costUsd)}` : "";
  const kind = t.kind === "cloud" ? "云端" : t.key ? `key ${t.key}` : "API";
  return `${t.id} · ${kind} · ${taskStateZh(t.state)}${cost}`;
}

module.exports = { state, changed, run, reset, refresh, available, enabled, taskLine };
