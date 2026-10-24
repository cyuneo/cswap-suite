// Keep Claude Code's "Dynamic workflows" (the Ultracode toggle) on for every
// account. Its default depends on the plan (off on Pro), and the /config
// toggle deletes `enableWorkflows` whenever the chosen value equals the
// current account's default, so after a cswap switch to a Pro account the
// toggle can vanish. settings.json is shared by all accounts; we only add the
// key when it is missing and never override an explicit false or a
// disableWorkflows the user set themselves.

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { cfg } = require("./config");

function settingsPath() {
  const base = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
  return path.join(base, "settings.json");
}

function ensureWorkflows() {
  if (!cfg("keepWorkflowsEnabled", true)) return false;
  const file = settingsPath();
  let data = {};
  let mode = 0o600;
  try {
    const text = fs.readFileSync(file, "utf8");
    mode = fs.statSync(file).mode & 0o777;
    data = text.trim() ? JSON.parse(text) : {};
  } catch (e) {
    if (e.code !== "ENOENT") return false; // unreadable or not plain JSON: leave it alone
    if (!fs.existsSync(path.dirname(file))) return false; // Claude Code not set up here
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  if ("enableWorkflows" in data || data.disableWorkflows === true) return false;
  data.enableWorkflows = true;
  const tmp = `${file}.cswap-${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", { mode });
  fs.renameSync(tmp, file);
  return true;
}

let noticeShown = false;

// Called from the scheduler tick.
function check() {
  let changedFile = false;
  try {
    changedFile = ensureWorkflows();
  } catch (e) {
    return false;
  }
  if (changedFile && !noticeShown) {
    noticeShown = true;
    vscode.window.showInformationMessage(
      "cswap：已为所有账号开启 Claude Code 的 Dynamic workflows（Ultracode 按钮）。如果按钮还没出现，关掉 Claude 面板再打开。"
    );
  }
  return changedFile;
}

module.exports = { settingsPath, ensureWorkflows, check };
