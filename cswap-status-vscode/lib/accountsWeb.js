// The 账号 view: a small web page in the cswap side bar with one compact card
// per account, its 5-hour and 7-day usage as rings. The page only shows HTML
// the extension builds here; its buttons post "switch" / "add" / "threshold"
// back, and right-click is VS Code's webview/context
// menu, which reads each card's data-vscode-context (package.json picks the
// items from cswapKind / cswapOn / cswapReserve), so every action is the same
// background cswap command as before.

const vscode = require("vscode");
const crypto = require("crypto");
const f = require("./format");
const accounts = require("./accounts");
const satellite = require("./satellite");
const manage = require("./manage");

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// What the right-click menu of a card offers. kind "other": can be switched
// to; "active" / "unswitchable" can still be edited; "none": not in cswap.
function cardContext(r) {
  const kind = r.active ? "active" : !r.number ? "none" : r.switchable === false ? "unswitchable" : "other";
  return {
    webviewSection: "account",
    accountNumber: r.number ? String(r.number) : "",
    cswapKind: kind,
    cswapOn: !(r.enabled === false || f.rowState(r) === "disabled"),
    cswapReserve: !!r.reserve,
    preventDefaultContextMenuItems: true,
  };
}

function resetText(resetsAt, now) {
  if (!resetsAt) return "";
  const left = Number(resetsAt) - now;
  if (left <= 0) return "已重置";
  return left < 60 ? "马上重置" : `${f.span(left)}后重置`;
}

// A point on the ring (radius r) at p percent, clockwise from the top.
function at(p, r) {
  const a = (p / 100) * 2 * Math.PI - Math.PI / 2;
  return [(20 + r * Math.cos(a)).toFixed(2), (20 + r * Math.sin(a)).toFixed(2)];
}

// One usage window as a ring: the window and its percentage inside, when it
// resets under it. The ring turns yellow within NEAR_MARGIN of the account's
// own line and red at it; a tick marks that line, where cswap switches away.
function ringHtml(label, name, used, max, source, resetsAt, now) {
  const known = used !== null && used !== undefined;
  const u = known ? Math.max(0, Math.min(100, Number(used))) : 0;
  const m = max === null || max === undefined ? null : Number(max);
  const level = !known || m === null ? "" : u >= m ? " over" : u >= m - f.NEAR_MARGIN ? " near" : "";
  let tick = "";
  if (m !== null && m < 100) {
    const [x1, y1] = at(m, 12.5);
    const [x2, y2] = at(m, 19.5);
    tick = `<line class="tick" data-limit="${m}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
  }
  const arc = u > 0 ? `<circle class="arc" cx="20" cy="20" r="16" pathLength="100" stroke-dasharray="${u} 100" transform="rotate(-90 20 20)"/>` : "";
  const left = resetsAt ? Number(resetsAt) - now : null;
  const cap = left === null ? "" : left <= 0 ? "已重置" : f.shortSpan(left);
  const tip = [
    `${name}：已用 ${f.pct(used)}%`,
    m !== null && `用到 ${f.pct(m)}% 切换${source === "own" ? "（这个账号自己的上限）" : source ? "（跟随阈值）" : ""}`,
    resetsAt && `${resetText(resetsAt, now)}（${f.dayClock(resetsAt)}）`,
  ];
  return `<div class="ring${level}" title="${esc(tip.filter(Boolean).join("\n"))}">
<svg viewBox="0 0 40 40" aria-hidden="true"><circle class="track" cx="20" cy="20" r="16"/>${arc}${tick}<text class="k" x="20" y="14.5">${label}</text><text x="20" y="24.5">${known ? `${Math.round(u)}%` : "?"}</text></svg>
<div class="cap">${cap}</div>
</div>`;
}

// "1时15分后可用" for an account at its limit: when its blocking window resets.
function backAt(r, now) {
  const at = r.blockedWindow === "5h" ? r.resetsAt5h : r.blockedWindow === "7d" ? r.resetsAt7d : null;
  if (!at || Number(at) <= now) return "";
  return `${f.shortSpan(Number(at) - now)}后可用`;
}

function cardHtml(r, now) {
  const st = f.rowState(r);
  const ctx = cardContext(r);
  const tags = [];
  if (r.active) tags.push('<span class="tag now">当前</span>');
  // the red ring already shows which window; other limits (e.g. opus) say so
  if (st === "limit") tags.push(`<span class="tag warn">${["5h", "7d"].includes(r.blockedWindow) ? "到上限" : esc(f.statusZh(r))}</span>`);
  else if (!["active", "ready", "reserve"].includes(st)) tags.push(`<span class="tag">${esc(f.statusZh(r))}</span>`);
  if (r.reserve) tags.push('<span class="tag main">主账号</span>');
  if (r.number && (r.lastError || r.quarantined)) tags.push(`<button data-cmd="relogin" data-n="${esc(r.number)}">重新登录</button>`);
  else if (st === "limit" || st === "off-schedule") {
    // switching there would be undone at once: say when it is usable instead
    const back = backAt(r, now);
    if (back) tags.push(`<span class="dim small">${esc(back)}</span>`);
  } else if (ctx.cswapKind === "other") tags.push(`<button data-cmd="switch" data-n="${esc(r.number)}">切换</button>`);
  const name = r.name || r.email || "?";
  const tip = [r.email && r.email !== name && r.email, f.accountDetail(r), r.reason].filter(Boolean).join("\n");
  const notes = [];
  if (r.lastError) notes.push(`读取用量失败（${r.lastError}）`);
  if (r.usageAgeSeconds > 3600 || (r.lastError && r.usageAgeSeconds > 0)) notes.push(`用量是 ${f.span(r.usageAgeSeconds)}前的`);
  if (r.quarantined) notes.push(`自动切换已隔离：${r.quarantined}`);
  return `<section class="card${ctx.cswapOn ? "" : " off"}" data-vscode-context="${esc(JSON.stringify(ctx))}">
<div class="info">
<div class="name" title="${esc(tip)}">${r.number ? `<span class="num">#${esc(r.number)}</span>` : ""}${esc(name)}</div>
${tags.length ? `<div class="tags">${tags.join("")}</div>` : ""}
${notes.map((n) => `<div class="note">${esc(n)}</div>`).join("")}
</div>
${ringHtml("5h", "5 小时", r.usage5h, r.max5h, r.max5hSource, r.resetsAt5h, now)}
${ringHtml("7d", "7 天", r.usage7d, r.max7d, r.max7dSource, r.resetsAt7d, now)}
</section>`;
}

// The global switch line, clickable to change it (cswapStatus.autoThreshold).
function thresholdHtml(a) {
  if (typeof a.threshold !== "number") return "";
  const weekly = typeof a.weeklyThreshold === "number" ? ` · 7d ${f.pct(a.weeklyThreshold)}%` : "";
  const tip = "自动切换阈值：没设自己上限的账号用到这里就切换。点击修改；单个账号在右键菜单「切换上限…」里设。";
  return `<button class="link" data-cmd="threshold" title="${esc(tip)}">阈值 ${f.pct(a.threshold)}%${weekly}</button>`;
}

// The page body for the accounts state of the last refresh.
function bodyHtml(state, opts) {
  const o = opts || {};
  const now = o.now || Date.now() / 1000;
  const w = o.where || f.where();
  const a = (state.data && state.data.autoswitch) || {};
  const out = [
    `<div class="top"><span class="where" title="${esc(state.bin ? `cswap：${state.bin}` : "")}">${esc(w.label)}</span>${
      state.loaded && !state.error ? thresholdHtml(a) : ""
    }</div>`,
  ];
  if (!state.loaded) out.push('<div class="dim">读取中…</div>');
  else if (state.error) out.push(`<div class="err">${esc(state.error)}</div>`);
  else if (!state.rows.length) out.push('<div class="empty">还没有账号<br><button data-cmd="add">添加账号</button></div>');
  else for (const r of state.rows) out.push(cardHtml(r, now));
  const note = o.note === undefined ? satellite.syncNote() : o.note;
  if (note) out.push(`<div class="warnline">${esc(note)}</div>`);
  return out.join("\n");
}

// bodyHtml that never throws: a bug in it shows on the page instead of a
// blank view.
function safeBody(state) {
  try {
    return bodyHtml(state);
  } catch (e) {
    return `<div class="err">账号面板出错：${esc((e && e.message) || e)}<br>请把这句话发给维护者；终端里 cswap list 仍可用。</div>`;
  }
}

const CSS = `
body { padding: 2px 12px 12px; color: var(--vscode-foreground); font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); }
.top { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }
.where { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--vscode-descriptionForeground); font-size: .9em; }
.card { display: flex; align-items: center; gap: 6px; padding: 7px 0; border-bottom: 1px solid rgba(128, 128, 128, .2); }
.card:last-of-type { border-bottom: none; }
.card.off { opacity: .55; }
.info { flex: 1; min-width: 0; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num { color: var(--vscode-descriptionForeground); margin-right: 5px; }
.tags { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 3px; }
.tag { font-size: .85em; padding: 0 6px; border-radius: 8px; white-space: nowrap; background: rgba(128, 128, 128, .18); color: var(--vscode-descriptionForeground); }
.tag.now { background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
.tag.main { color: var(--vscode-foreground); }
.tag.warn { color: var(--vscode-editorWarning-foreground); }
button { font: inherit; font-size: .85em; padding: 0 7px; border: none; border-radius: 3px; cursor: pointer; color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); }
button:hover { background: var(--vscode-button-secondaryHoverBackground); }
button.link { padding: 0; background: none; color: var(--vscode-textLink-foreground); white-space: nowrap; }
button.link:hover { background: none; text-decoration: underline; }
.ring { flex: none; width: 44px; text-align: center; }
.ring svg { display: block; width: 40px; height: 40px; margin: 0 auto; }
.ring circle { fill: none; stroke-width: 4; }
.ring .track { stroke: rgba(128, 128, 128, .22); }
.ring .arc { stroke: var(--vscode-progressBar-background); }
.ring.near .arc { stroke: var(--vscode-charts-yellow); }
.ring.over .arc { stroke: var(--vscode-charts-red); }
.ring .tick { stroke: var(--vscode-foreground); stroke-width: 1.5; opacity: .55; }
.ring text { font-size: 10px; text-anchor: middle; dominant-baseline: central; fill: var(--vscode-foreground); }
.ring text.k { font-size: 7.5px; fill: var(--vscode-descriptionForeground); }
.cap { min-height: 12px; font-size: 10px; line-height: 1.2; margin: 1px -3px 0; white-space: nowrap; color: var(--vscode-descriptionForeground); }
.dim, .note, .empty { color: var(--vscode-descriptionForeground); }
.small { font-size: .85em; }
.note { font-size: .85em; margin-top: 3px; }
.err { color: var(--vscode-errorForeground); margin-top: 6px; }
.warnline { color: var(--vscode-editorWarning-foreground); font-size: .9em; margin-top: 8px; }
.empty { padding: 8px 0; }
.empty button { margin-top: 8px; padding: 1px 8px; }
`;

const SCRIPT = `
const vscode = acquireVsCodeApi();
const root = document.getElementById("root");
window.addEventListener("message", (e) => {
  if (e.data && e.data.type === "render") root.innerHTML = e.data.html;
});
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cmd]");
  if (b) vscode.postMessage({ type: b.dataset.cmd, n: b.dataset.n || "" });
});
vscode.postMessage({ type: "ready" });
`;

function pageHtml(body) {
  const nonce = crypto.randomBytes(16).toString("hex");
  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style nonce="${nonce}">${CSS}</style>
</head><body data-vscode-context='{"preventDefaultContextMenuItems": true}'>
<main id="root">${body}</main>
<script nonce="${nonce}">${SCRIPT}</script>
</body></html>`;
}

class AccountsView {
  constructor() {
    this.view = null;
  }
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = pageHtml(safeBody(accounts.state));
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    view.onDidDispose(() => {
      if (this.view === view) this.view = null;
    });
  }
  // The page is kept and only its body replaced, so a refresh does not
  // reload it (a hidden view gets the latest body when it says "ready").
  update() {
    if (this.view) this.view.webview.postMessage({ type: "render", html: safeBody(accounts.state) });
  }
  onMessage(m) {
    if (!m) return;
    if (m.type === "ready") return this.update();
    if (m.type === "switch") return accounts.switchAccount({ accountNumber: m.n });
    if (m.type === "relogin") return manage.relogin({ accountNumber: m.n });
    if (m.type === "add") return manage.addAccount();
    if (m.type === "threshold") return vscode.commands.executeCommand("cswapStatus.autoThreshold");
  }
}

module.exports = { AccountsView, bodyHtml, safeBody, cardContext, pageHtml };
