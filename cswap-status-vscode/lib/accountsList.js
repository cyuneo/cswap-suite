// The 账号 view as a list (VS Code's own tree): the same accounts as the ring
// cards in accountsWeb.js, for when VS Code cannot show a web page in the
// side bar (accountsMode.js picks which one shows). One row per account with
// its usage as a small double ring (ringIcon.js); right-click offers the same
// actions as on a card (package.json view/item/context, matched on the
// contextValue words below), and an inline button switches.

const vscode = require("vscode");
const f = require("./format");
const accounts = require("./accounts");
const satellite = require("./satellite");
const { cardContext, backAt } = require("./accountsWeb");
const ringIcon = require("./ringIcon");

let iconDir = null; // where the ring icons are written (the extension's storage)
function setIconDir(dir) {
  iconDir = dir || null;
  ringIcon.pruneNow(iconDir);
}

// "cswapAccount kind-other on reserve limit relogin": what the menus match
// with =~ ("limit": at a limit or outside its hours, so no inline 切换 button;
// "relogin": its usage cannot be read, so the inline button logs in again,
// like the card's 重新登录)
function contextValue(r) {
  const c = cardContext(r);
  const st = f.rowState(r);
  const blocked = st === "limit" || st === "off-schedule";
  const relogin = r.number && (r.lastError || r.quarantined);
  return ["cswapAccount", `kind-${c.cswapKind}`, c.cswapOn ? "on" : "off", c.cswapReserve && "reserve", blocked && "limit", relogin && "relogin"]
    .filter(Boolean)
    .join(" ");
}

// "5h 27/40%": the account's own line after the slash; the shared threshold
// is on the row above, so "5h 27%" (older cswap does not say which: shown)
function windowText(label, used, max, source) {
  const line = max !== null && max !== undefined && source !== "threshold";
  return `${label} ${f.pct(used)}${line ? `/${f.pct(max)}` : ""}%`;
}

// A row is narrow: an email shows as the part before the @, unless another
// account shares it (the full address is in the tooltip).
function shortName(r, rows) {
  const name = String(r.name || r.email || "?");
  const at = name.indexOf("@");
  if (at <= 0) return name;
  const local = name.slice(0, at);
  const same = (rows || []).filter((o) => String(o.name || o.email || "").split("@")[0] === local);
  return same.length > 1 ? name : local;
}

function accountItem(r, now, rows) {
  const st = f.rowState(r);
  const name = r.name || r.email || "?";
  const it = new vscode.TreeItem(`${r.number ? `#${r.number} ` : ""}${shortName(r, rows)}`);
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
  it.description = [windowText("5h", r.usage5h, r.max5h, r.max5hSource), windowText("7d", r.usage7d, r.max7d, r.max7dSource), ...tags].join(" · ");
  const files = ringIcon.iconFiles(iconDir, r, !cardContext(r).cswapOn);
  if (files) it.iconPath = { light: vscode.Uri.file(files.light), dark: vscode.Uri.file(files.dark) };
  else
    it.iconPath = new vscode.ThemeIcon(
      r.lastError || r.quarantined ? "warning" : st === "limit" ? "error" : st === "disabled" ? "circle-slash" : r.active ? "pass-filled" : "account"
    );
  it.tooltip = [
    name,
    r.email && r.email !== name && r.email,
    f.accountDetail(r),
    r.reason,
    r.lastError && `读取用量失败（${r.lastError}）`,
    "图标：外圈 5 小时，内圈 7 天；接近这个账号的切换线变黄，到线变红",
  ]
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
    ringIcon.pruneNow(iconDir);
    for (const r of state.rows) out.push(accountItem(r, now, state.rows));
    const note = satellite.syncNote();
    if (note) out.push(plainItem(note, "warning"));
    return out;
  }
}

module.exports = { AccountsList, accountItem, contextValue, setIconDir };
