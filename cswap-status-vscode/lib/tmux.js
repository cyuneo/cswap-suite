// Starts the official `claude` inside a tmux session so the same conversation
// can be driven from this terminal and from cswap-remote on the phone. Only
// spawns processes; reads and writes no Claude files or credentials.

const vscode = require("vscode");
const path = require("path");
const { findBin, localBin, shq } = require("./proc");
const { defaultCwd } = require("./format");

// The tmux argv for a terminal: run claude, then drop to a shell so the
// window stays open.
function tmuxArgs(claude, name, cwd, cont) {
  const inner = `${shq(claude)}${cont ? " --continue" : ""}; exec "\${SHELL:-/bin/sh}"`;
  return ["new-session", "-A", "-s", name, "-c", cwd, inner];
}

async function startInTmux(cont) {
  const tmux = findBin("tmux", [localBin()]);
  const claude = findBin("claude", [localBin()]);
  if (!tmux) {
    vscode.window.showErrorMessage("找不到 tmux。请先安装 tmux（sudo apt install tmux，或用 conda 安装）。");
    return;
  }
  if (!claude) {
    vscode.window.showErrorMessage("找不到 claude 命令（~/.local/bin/claude 或 PATH）。");
    return;
  }
  const folders = vscode.workspace.workspaceFolders || [];
  let cwd = defaultCwd();
  if (folders.length > 1) {
    const pick = await vscode.window.showQuickPick(
      folders.map((f) => ({ label: f.name, description: f.uri.fsPath, cwd: f.uri.fsPath })),
      { placeHolder: "在哪个目录启动 Claude？" }
    );
    if (!pick) return;
    cwd = pick.cwd;
  }
  const slug = (path.basename(cwd) || "home").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24) || "claude";
  const name = await vscode.window.showInputBox({
    title: cont ? "在 tmux 里接着上次的对话" : "在 tmux 里启动 Claude",
    prompt: cont
      ? "tmux 会话名。会接着这个目录最近的对话：如果它还开在 VS Code 面板里，请先关掉那个面板。"
      : "tmux 会话名。已经有同名会话时会直接接上它，不会再开一个。",
    value: `cr-${slug}`,
    validateInput: (v) => (/^[A-Za-z0-9_-]{1,40}$/.test(v) ? null : "只能用字母、数字、- 和 _"),
  });
  if (!name) return;
  const term = vscode.window.createTerminal({
    name: `Claude (tmux ${name})`,
    cwd,
    shellPath: tmux,
    shellArgs: tmuxArgs(claude, name, cwd, cont),
  });
  term.show();
  vscode.window.setStatusBarMessage(
    `已在 tmux 会话 ${name} 里启动 Claude，手机上的 Claude Remote 可以操作它。关掉这个终端不会结束会话（tmux attach -t ${name} 接回来）。`,
    15000
  );
}

module.exports = { tmuxArgs, startInTmux };
