// The account status bar entry and the palette quick pick (the 账号 view
// itself is the web page in accountsWeb.js).

const vscode = require("vscode");
const { runtime, barMode, showItem } = require("./config");
const f = require("./format");
const accounts = require("./accounts");
const satellite = require("./satellite");
const autoswitch = require("./autoswitch");
const tmux = require("./tmux");

let item = null;

function init(statusItem) {
  item = statusItem;
}

function render() {
  if (!item) return;
  const { state } = accounts;
  const w = f.where();
  const compact = barMode() === "compact";
  // In compact mode the machine shows only as an icon, and only when remote.
  const prefix = compact ? (w.remote ? "$(remote) " : "") : `${w.icon} ${w.label} │ `;
  item.backgroundColor = undefined;
  if (state.error) {
    item.text = `${prefix}$(warning) cswap?`;
    item.tooltip = `${w.label}\n${state.error}`;
    showItem(item);
    return;
  }
  const rows = state.rows;
  const active = rows.find((r) => r.active);
  let account;
  let warn = false;
  if (!rows.length) account = "未添加账号";
  else if (!active) account = compact ? "未管理" : "当前登录未被 cswap 管理";
  else {
    account = compact ? `#${active.number} ${f.compactUsage(active)}` : `${active.name} ${f.usageText(active)}`;
    if (f.isWarn(active)) {
      if (!compact) account += ` ${f.statusZh(active)}`;
      warn = true;
    }
  }
  const marker = autoswitch.statusMarker();
  item.text = `${prefix}$(account) ${account}${marker.text}`;
  if (warn || marker.warn) item.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
  const md = new vscode.MarkdownString();
  md.supportThemeIcons = true;
  md.appendMarkdown(`**${w.remote ? "远程" : "本机"}：${w.label}**  \n`);
  md.appendMarkdown(`cswap：\`${state.bin}\`\n\n`);
  md.appendMarkdown("| 账号 | 5h | 7d | 状态 |\n|---|---|---|---|\n");
  for (const r of rows) {
    const name = `${r.active ? "▶ " : ""}${r.number ?? "-"}: ${r.name}`;
    md.appendMarkdown(`| ${name} | ${f.pct(r.usage5h)}/${f.pct(r.max5h)}% | ${f.pct(r.usage7d)}/${f.pct(r.max7d)}% | ${f.statusZh(r)} |\n`);
  }
  if (marker.tip) md.appendMarkdown(`\n${marker.text.trim()} ${marker.tip}\n`);
  const note = satellite.syncNote();
  if (note) md.appendMarkdown(`\n$(warning) ${note}\n`);
  md.appendMarkdown("\n点击打开 cswap 侧栏");
  item.tooltip = md;
  showItem(item);
}

// Palette only (cswapStatus.show): accounts plus the less common actions.
async function show() {
  await runtime.refreshAccounts();
  const w = f.where();
  const picks = accounts.state.rows
    .filter((r) => r.number)
    .map((r) => ({
      label: `${r.active ? "$(check) " : ""}${r.number}: ${r.name}`,
      description: f.usageText(r),
      detail: f.accountDetail(r),
      row: r,
    }));
  picks.push({ label: "$(dashboard) 在终端打开 cswap 仪表盘", action: "tui" });
  picks.push({ label: "$(add) 添加账号…", action: "add" });
  picks.push({ label: "$(device-mobile) 在 tmux 里启动 Claude（手机可操作）", action: "tmux" });
  picks.push({ label: "$(history) 在 tmux 里接着这个目录上次的对话", action: "tmux-continue" });
  const choice = await vscode.window.showQuickPick(picks, {
    title: `cswap @ ${w.label}`,
    placeHolder: "选择账号以手动切换",
  });
  if (!choice) return;
  if (choice.action === "add") return require("./manage").addAccount();
  if (choice.action === "tmux") return tmux.startInTmux(false);
  if (choice.action === "tmux-continue") return tmux.startInTmux(true);
  if (choice.action === "tui") return accounts.cswapInTerminal([]);
  return accounts.switchAccount({ accountNumber: choice.row.number, row: choice.row });
}

module.exports = { init, render, show };
