// Accounts from the server, via the bundled cswap-satellite
// (extension/satellite/cswap_satellite.py, copied in by build-vsix.py): the
// server's `cswap list --json` (no tokens) tells which accounts exist; each
// setup-token is typed into a password box and piped to `cswap add-token -`.
// The token is never written to settings, logs or files by this extension.

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { runtime, cfg } = require("./config");
const { findBin, resolveBin, exec, lastLine } = require("./proc");
const { where } = require("./format");
const accounts = require("./accounts");

const SYNC_EVERY_MS = 5 * 60 * 1000;
const sync = { last: 0, note: "" };

function satelliteServer() {
  return String(cfg("satelliteServer", "") || "").trim();
}

function scriptPath() {
  const packaged = path.join(runtime.extPath, "satellite", "cswap_satellite.py");
  if (fs.existsSync(packaged)) return packaged;
  // a source checkout next to cswap-satellite/ (the package gets a copy)
  const sibling = path.join(runtime.extPath, "..", "cswap-satellite", "cswap_satellite.py");
  return fs.existsSync(sibling) ? sibling : null;
}

function pythonFor() {
  // cswap's own interpreter first (a uv venv on most machines; avoids the
  // macOS /usr/bin/python3 stub that asks to install developer tools).
  const cands = [];
  try {
    const real = fs.realpathSync(accounts.state.bin || resolveBin("path", "cswap") || "");
    const first = fs.readFileSync(real, "utf8").split("\n", 1)[0];
    const m = first.match(/^#!\s*(\S+python[\d.]*)\s*$/);
    if (m) cands.push(m[1]);
  } catch (e) {
    /* fall through */
  }
  const sys = findBin("python3", ["/opt/homebrew/bin", "/usr/local/bin"]);
  if (sys) cands.push(sys);
  return cands.find((c) => fs.existsSync(c)) || null;
}

async function runSatellite(args, input) {
  const py = pythonFor();
  if (!py) return { code: -1, out: "", err: "找不到 Python 3。" };
  const script = scriptPath();
  if (!script) return { code: -1, out: "", err: "扩展包里缺少 satellite/cswap_satellite.py，请重新安装扩展。" };
  const bin = accounts.state.bin || resolveBin("path", "cswap");
  const extra = bin ? ["--cswap", bin] : [];
  const r = await exec(py, [script, ...args, ...extra], { input: input || "", timeout: 180000 });
  return { code: r.code, out: r.out.trim(), err: r.timedOut ? "cswap-satellite 超时（3 分钟）" : r.err };
}

function parsePlan(out) {
  try {
    const data = JSON.parse(out);
    return data && Array.isArray(data.accounts) ? data.accounts : null;
  } catch (e) {
    return null;
  }
}

async function addAccounts() {
  if (where().remote) {
    const go = await vscode.window.showWarningMessage(
      `这个窗口连着远程机器 ${os.hostname()}，账号会加到这台远程机器的 cswap 里（不是你面前这台电脑）。继续吗？`,
      { modal: true },
      "继续"
    );
    if (go !== "继续") return;
  }
  let server = satelliteServer();
  if (!server) {
    server = (
      (await vscode.window.showInputBox({
        title: "从服务器添加账号",
        prompt: "服务器的 SSH 主机名（这台电脑上 ssh 能免密码直接连上的名字，比如 Tailscale 地址）",
        ignoreFocusOut: true,
        validateInput: (v) => (/^[^\s-][^\s]*$/.test(v.trim()) ? null : "填 ssh 用的主机名"),
      })) || ""
    ).trim();
    if (!server) return;
  }
  const plan = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `读取 ${server} 上的账号…` },
    () => runSatellite(["plan", "--server", server])
  );
  if (plan.code !== 0) {
    vscode.window.showErrorMessage(lastLine(plan.err) || lastLine(plan.out) || "读取服务器账号失败");
    return;
  }
  const list = parsePlan(plan.out);
  if (!list) {
    vscode.window.showErrorMessage(`cswap-satellite plan 的输出无法解析：${lastLine(plan.out).slice(0, 200)}`);
    return;
  }
  // remember the server only once it answered with a usable plan
  await vscode.workspace.getConfiguration("cswapStatus").update("satelliteServer", server, vscode.ConfigurationTarget.Global);
  const results = [];
  for (const a of list) {
    if (a.action === "skip") {
      results.push(`跳过账号 ${a.number}：${a.reason}`);
      continue;
    }
    const tok = await vscode.window.showInputBox({
      title: `账号 ${a.number}（${a.email}）${a.action === "update" ? "：换新 token" : ""}`,
      prompt: "粘贴这个账号的 setup-token（sk-ant-oat01-…，用 claude setup-token 生成）。不会显示；留空跳过，Esc 停止。",
      password: true,
      ignoreFocusOut: true,
      validateInput: (v) =>
        !v.trim() || /^sk-ant-oat01-[A-Za-z0-9_-]{20,}$/.test(v.trim())
          ? null
          : v.trim().startsWith("sk-ant-api")
          ? "这是按量付费的 API key，不是订阅的 setup-token"
          : "应该是 sk-ant-oat01- 开头的一整串",
    });
    if (tok === undefined) break;
    if (!tok.trim()) {
      results.push(`跳过账号 ${a.number}`);
      continue;
    }
    const r = await runSatellite(["add", "--account", String(a.number), "--server", server], tok.trim() + "\n");
    results.push(r.code === 0 ? r.out : lastLine(r.err) || lastLine(r.out) || `账号 ${a.number} 失败`);
  }
  if (results.length) {
    vscode.window.showInformationMessage(results.join("；"), { modal: true });
  }
  sync.last = 0;
  runtime.refreshAccounts();
}

// Called from the scheduler tick. satelliteServer is a machine-scoped
// setting, so it is only set on the machines that use tokens (local or
// reached over Remote-SSH alike).
function maybeSync() {
  const server = satelliteServer();
  if (!server || Date.now() - sync.last < SYNC_EVERY_MS) return null;
  sync.last = Date.now();
  return runSatellite(["sync-usage", "--quiet", "--server", server]).then((r) => {
    sync.note = r.code === 0 ? "" : `从 ${server} 同步用量失败：${lastLine(r.err) || lastLine(r.out)}`;
    if (r.code === 0) setTimeout(() => runtime.refreshAccounts(), 1000);
    else accounts.changed.fire();
    return r;
  });
}

const syncNote = () => sync.note;

module.exports = { satelliteServer, scriptPath, pythonFor, runSatellite, parsePlan, addAccounts, maybeSync, syncNote, _sync: sync };
