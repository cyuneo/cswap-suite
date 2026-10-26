// The 账号 view as a plain list (cswapStatus.accountsStyle = "list"): the
// same accounts as the ring cards in accountsWeb.js, for when VS Code cannot
// show a web page in the side bar. One row per account; right-click offers
// the same actions as on a card (package.json view/item/context, matched on
// the contextValue words below), and an inline button switches.

const vscode = require("vscode");
const f = require("./format");
const accounts = require("./accounts");
const satellite = require("./satellite");
const { cardContext, backAt } = require("./accountsWeb");

// "cswapAccount kind-other on reserve limit": what the menus match with =~
// ("limit": at a limit or outside its hours, so no inline 切换 button)
function contextValue(r) {
  const c = cardContext(r);
  const st = f.rowState(r);
  const blocked = st === "limit" || st === "off-schedule";
  return ["cswapAccount", `kind-${c.cswapKind}`, c.cswapOn ? "on" : "off", c.cswapReserve && "reserve", blocked && "limit"].filter(Boolean).join(" ");
}

function windowText(label, used, max) {
  return `${label} ${f.pct(used)}${max === null || max === undefined ? "" : `/${f.pct(max)}`}%`;
}

function accountItem(r, now) {
  const st = f.rowState(r);
  const name = r.name || r.email || "?";
  const it = new vscode.TreeItem(`${r.number ? `#${r.number} ` : ""}${name}`);
  it.accountNumber = r.number ? String(r.number) : "";
  it.contextValue = contextValue(r);
  const tags = [];
  if (r.active) tags.push("当前");
  if (r.reserve) tags.push("主账号");
  if (st === "limit" || st === "off-schedule") {
    tags.push(st === "limit" && ["5h", "7d"].includes(r.blockedWindow) ? "到上限" : f.statusZh(r));
    const back = backAt(r, now);
    if (back) tags.push(back);
  } else if (!["active", "ready", "reserve"].includes(st)) tags.push(f.statusZh(r));
  if (r.lastError) tags.push("读取用量失败");
  it.description = [windowText("5h", r.usage5h, r.max5h), windowText("7d", r.usage7d, r.max7d), ...tags].join(" · ");
  const icon =
    r.lastError || r.quarantined ? "warning" : st === "limit" ? "error" : st === "disabled" ? "circle-slash" : r.active ? "pass-filled" : "account";
  it.iconPath = new vscode.ThemeIcon(icon);
  it.tooltip = [r.email && r.email !== name && r.email, f.accountDetail(r), r.reason, r.lastError && `读取用量失败（${r.lastError}）`]
    .filter(Boolean)
    .join("\n");
  return it;
}

function plainItem(label, icon, command, tooltip) {
  const it = new vscode.TreeItem(label);
  if (icon) it.iconPath = new vscode.ThemeIcon(icon);
  if (command) it.command = { command, title: label };
  if (tooltip) it.tooltip = tooltip;
  return it;
}

class AccountsList {
  constructor() {
    this.onDidChangeTreeData = accounts.changed.event;
  }
  getTreeItem(node) {
    return node;
  }
  getChildren(node) {
    if (node) return [];
    const { state } = accounts;
    const now = Date.now() / 1000;
    if (!state.loaded) return [plainItem("读取中…", "loading~spin")];
    if (state.error) return [plainItem(state.error, "warning")];
    const out = [];
    const a = (state.data && state.data.autoswitch) || {};
    if (typeof a.threshold === "number") {
      const weekly = typeof a.weeklyThreshold === "number" ? ` · 7d ${f.pct(a.weeklyThreshold)}%` : "";
      out.push(plainItem(`阈值 ${f.pct(a.threshold)}%${weekly}`, "dashboard", "cswapStatus.autoThreshold", "自动切换阈值：没设自己上限的账号用到这里就切换。点击修改。"));
    }
    if (!state.rows.length) out.push(plainItem("还没有账号：点这里添加", "add", "cswapStatus.addAccount"));
    for (const r of state.rows) out.push(accountItem(r, now));
    const note = satellite.syncNote();
    if (note) out.push(plainItem(note, "warning"));
    return out;
  }
}

module.exports = { AccountsList, accountItem, contextValue };
