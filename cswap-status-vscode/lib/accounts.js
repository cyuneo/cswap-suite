// Account data: one `cswap policy list --json` per refresh (cswap's local
// usage store only, no network, no tokens). The same JSON also carries the
// `autoswitch` block that the 自动切换 view reads, so no second process.

const vscode = require("vscode");
const { runtime } = require("./config");
const { resolveBin, exec, lastLine, shq } = require("./proc");
const { where, switchBlockReason } = require("./format");

const state = {
  rows: [],
  data: null, // the whole policy list JSON of the last good read
  bin: null, // the cswap that was run (also kept when it failed)
  error: "",
  loaded: false, // the first refresh has finished
};
const changed = new vscode.EventEmitter();

async function fetchRows() {
  const bin = resolveBin("path", "cswap");
  if (!bin)
    return {
      bin: null,
      error: `cswap 未找到（cswapStatus.path、~/.local/bin/cswap 或 PATH）${vscode.env.remoteName ? "；远程窗口要在“远程”设置里填 cswapStatus.path" : ""}`,
    };
  const r = await exec(bin, ["policy", "list", "--json"], { timeout: 20000 });
  if (r.timedOut) return { bin, error: "cswap policy list 超时（20 秒）" };
  const start = r.out.indexOf("{");
  if (start < 0) {
    if (r.code !== 0) return { bin, error: `cswap policy list 失败：${lastLine(r.err) || `退出码 ${r.code}`}` };
    return { bin, error: "cswap policy list 没有输出 JSON（这个 cswap 可能不含 policy 命令）" };
  }
  try {
    const data = JSON.parse(r.out.slice(start));
    return { bin, data, rows: Array.isArray(data.accounts) ? data.accounts : [] };
  } catch (e) {
    return { bin, error: `cswap policy list 的输出无法解析：${lastLine(String(e.message || e))}` };
  }
}

async function refresh() {
  const res = await fetchRows();
  state.bin = res.bin;
  state.loaded = true;
  if (res.error) {
    state.rows = [];
    state.data = null;
    state.error = res.error;
  } else {
    state.rows = res.rows;
    state.data = res.data;
    state.error = "";
  }
  changed.fire();
  return state;
}

// Runs one state-changing cswap command in the background with a progress
// notification (`quiet`: a status bar spinner instead), then refreshes.
// Resolves the result, or null after showing the failure. `input` goes to
// stdin (a pasted token, or "y" for a prompt).
async function runCswap(args, opts) {
  const o = opts || {};
  const bin = state.bin || resolveBin("path", "cswap");
  if (!bin) {
    vscode.window.showErrorMessage(state.error || "cswap 未找到");
    return null;
  }
  const r = await vscode.window.withProgress(
    { location: o.quiet ? vscode.ProgressLocation.Window : vscode.ProgressLocation.Notification, title: o.title || `cswap ${args[0]}…` },
    () => exec(bin, args, { timeout: o.timeout || 60000, input: o.input, env: { ...process.env, NO_COLOR: "1" } })
  );
  runtime.refreshAccounts();
  if (r.code !== 0) {
    vscode.window.showErrorMessage(`cswap ${args[0]} 失败：${lastLine(r.err) || lastLine(r.out) || `退出码 ${r.code}`}`);
    return null;
  }
  return r;
}

// The dashboard is a full-screen TUI, so it still opens in a terminal.
function cswapInTerminal(args) {
  const bin = state.bin || resolveBin("path", "cswap");
  if (!bin) return vscode.window.showErrorMessage(state.error || "cswap 未找到");
  const w = where();
  const term = vscode.window.createTerminal({ name: `cswap (${w.label})` });
  term.show();
  term.sendText([shq(bin), ...args].join(" "));
  setTimeout(() => runtime.refreshAccounts(), 5000);
}

// ⇄ in the 账号 view. A target that is disabled, over its line or outside its
// schedule would be switched away from again by cswap auto, so ask first.
async function switchAccount(node) {
  const n = node && node.accountNumber;
  if (!n || !/^\d+$/.test(String(n))) return;
  const row = (node && node.row) || state.rows.find((r) => String(r.number) === String(n));
  const why = row && switchBlockReason(row);
  if (why) {
    const ok = await vscode.window.showWarningMessage(
      `自动切换会在约 1 分钟内切回，因为 ${why}。仍要切换？`,
      { modal: true },
      "仍要切换"
    );
    if (ok !== "仍要切换") return;
  }
  // One quiet confirmation: no pop-up here, and the 自动切换 view skips its
  // "cswap 已切换" pop-up for this switch.
  runtime.ownSwitchAt = Date.now();
  const r = await runCswap(["switch", String(n)], { title: `切换到 #${n}…`, quiet: true });
  if (r) vscode.window.setStatusBarMessage(`$(check) 已切换到 #${n}，下一条消息起生效，不用重启 Claude`, 8000);
}

module.exports = { state, changed, fetchRows, refresh, runCswap, cswapInTerminal, switchAccount };
