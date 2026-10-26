// The 账号 view (the webview page: cards, usage rings, buttons, right-click items),
// the account status bar entry, switching, cswap error reporting, the
// satellite add/sync flow and tmux argv, against the stub `vscode` module and
// fake programs.
//   node test/accounts.test.js
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const stub = require("./vscode-stub");
const { tmpDir, fakeProgram, hermetic } = require("./fakes");

const tmp = tmpDir("accounts");
hermetic(tmp); // no real ~/.local/bin/cswap, api-worker or systemctl
const cswap = fakeProgram(tmp, "cswap");
const extDir = path.join(tmp, "ext");
fs.mkdirSync(path.join(extDir, "satellite"), { recursive: true });
const sat = fakeProgram(path.join(extDir, "satellite"), "cswap_satellite.py");
Object.assign(stub.config, { path: cswap.bin, showApi: false, refreshSeconds: 60, keepWorkflowsEnabled: false, statusBar: "compact" });
stub.vscode.workspace.workspaceFolders = [{ name: "w", uri: { fsPath: tmp } }];
const { shown, answers, statusItems, terminals, executed } = stub;

const ext = require("../extension.js");
const T = ext._test;
const pkg = require("../package.json");

const ACCOUNTS = [
  { number: "1", name: "a@x", active: false, enabled: true, usage5h: 2, usage7d: 26, max5h: 98, max7d: 98, status: "Reserve", reserve: true, schedule: [] },
  { number: "2", name: "b@x", active: false, enabled: false, usage5h: 29, usage7d: 23, max5h: 5, max7d: 98, status: "Disabled", reason: "personal 5h limit" },
  { number: "3", name: "c@x", active: true, enabled: true, usage5h: 34, usage7d: 20, max5h: 98, max7d: 98, status: "Active" },
  { number: "4", name: "d@x", active: false, enabled: true, usage5h: 95, usage7d: 20, max5h: 98, max7d: 98, status: "Ready" },
  // 60% is fine against a fixed 90 but within 15 points of its own 70% line
  { number: "5", name: "e@x", active: false, enabled: true, usage5h: 60, usage7d: 10, max5h: 70, max7d: 98, status: "Ready" },
  // a model-scoped limit, old cswap (status text only)
  { number: "6", name: "f@x", active: false, enabled: true, usage5h: 10, usage7d: 10, max5h: 98, max7d: 98, status: "Policy-limit (opus)" },
];
// Every other cswap command the side bar runs succeeds with a short line.
const OK = { switch: { out: "Switched to Account-1" }, add: { out: "Added Account 7: g@x [personal]" } };
const setRows = (rows) =>
  cswap.set({ ...OK, "policy list": { out: { schemaVersion: 1, accounts: rows, autoswitch: { threshold: 98 } } } });
const switchCalls = () => cswap.calls().filter((c) => c.argv[0] === "switch").map((c) => c.argv.join(" "));

// The 账号 page: the body last posted to it, split into cards.
const view = stub.fakeWebviewView();
const page = () => (view.webview.posted.length ? view.webview.posted[view.webview.posted.length - 1].html : "");
const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const text = (h) => unesc(h.replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
function cards() {
  const out = {};
  for (const m of page().matchAll(/<section class="card( off)?" data-vscode-context="([^"]*)">([\s\S]*?)<\/section>/g)) {
    const ctx = JSON.parse(unesc(m[2]));
    out[ctx.accountNumber] = { off: !!m[1], ctx, html: m[3], text: text(m[3]) };
  }
  return out;
}
const arcs = (c) => [...c.html.matchAll(/stroke-dasharray="([\d.]+) 100"/g)].map((m) => Number(m[1]));
const ticks = (c) => [...c.html.matchAll(/data-limit="([\d.]+)"/g)].map((m) => Number(m[1]));
const levels = (c) => [...c.html.matchAll(/class="ring( \w+)?"/g)].map((m) => (m[1] || "").trim());
// The right-click items VS Code would show for a card: package.json's
// webview/context when-clauses against the card's data-vscode-context.
function whenTrue(expr, ctx) {
  return expr.split("&&").every((part) => {
    const p = part.trim();
    let m;
    if ((m = p.match(/^(\w+) (==|!=) '([^']*)'$/))) return (String(ctx[m[1]]) === m[3]) === (m[2] === "==");
    if ((m = p.match(/^(!?)(\w+)$/))) return !!ctx[m[2]] !== (m[1] === "!");
    throw new Error(`when-clause not understood: ${p}`);
  });
}
const menu = (c) =>
  pkg.contributes.menus["webview/context"]
    .filter((m) => whenTrue(m.when, { webviewId: "cswapStatus.accounts", ...c.ctx }))
    .map((m) => m.command.replace("cswapStatus.", ""));

(async () => {
  setRows(ACCOUNTS);
  ext.activate({ extensionPath: extDir, subscriptions: [] });
  // the 账号 view is a webview; before the first refresh it says it is loading
  stub.webviews["cswapStatus.accounts"].resolveWebviewView(view);
  assert.ok(view.webview.options.enableScripts);
  assert.match(view.webview.html, /读取中…/);
  // a page that fails to build says why instead of staying blank
  const web = require("../lib/accountsWeb");
  assert.match(web.safeBody({ loaded: true, rows: null }), /账号面板出错/);
  // what the page did goes to accounts-view.log in the extension's log folder
  const logDir = require("fs").mkdtempSync(require("path").join(require("os").tmpdir(), "cswap-log-"));
  web.setLogDir(logDir);
  const probe = new web.AccountsView();
  const pv = stub.fakeWebviewView();
  probe.resolveWebviewView(pv);
  probe.onMessage({ type: "ready", size: "10 chars, 0px tall" });
  probe.onMessage({ type: "pageError", msg: "boom" });
  const logged = require("fs").readFileSync(require("path").join(logDir, "accounts-view.log"), "utf8");
  assert.match(logged, /resolve: visible=/);
  assert.match(logged, /ready: 10 chars, 0px tall/);
  assert.match(logged, /pageError: boom/);
  // which 账号 view shows: cards until a window's page fails to load ("auto")
  const mode = require("../lib/accountsMode");
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], false, "cards at activation");
  // auto: a page that never says "ready" is logged, and this window switches
  // to the list by itself (no question, no setting changed)
  const stuck = new web.AccountsView({ readyWaitMs: 20 });
  stuck.resolveWebviewView(stub.fakeWebviewView());
  await stub.sleep(60);
  assert.match(require("fs").readFileSync(require("path").join(logDir, "accounts-view.log"), "utf8"), /no-ready/);
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], true);
  assert.match(shown.status[shown.status.length - 1], /改用列表显示/);
  assert.strictEqual(stub.configUpdates.length, 0);
  // "rings": the cards stay, and one offer to switch to the list
  mode._reset();
  stub.config.accountsStyle = "rings";
  await mode.apply();
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], false);
  answers.warn.push("改用列表显示");
  new web.AccountsView({ readyWaitMs: 20 }).resolveWebviewView(stub.fakeWebviewView());
  await stub.sleep(60);
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], false);
  assert.match(shown.warn[shown.warn.length - 1], /账号面板没有加载出来/);
  await stub.sleep(10);
  assert.deepStrictEqual(stub.configUpdates[stub.configUpdates.length - 1].slice(0, 2), ["accountsStyle", "list"]);
  await mode.apply();
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], true, "list");
  stub.config.accountsStyle = "rings";
  const warns = shown.warn.length;
  new web.AccountsView({ readyWaitMs: 20 }).resolveWebviewView(stub.fakeWebviewView());
  await stub.sleep(60);
  assert.strictEqual(shown.warn.length, warns, "offered once per window");
  // an unknown value counts as auto
  stub.config.accountsStyle = "bogus";
  assert.strictEqual(mode.style(), "auto");
  // a page that loads in time: no timer left
  const fine = new web.AccountsView({ readyWaitMs: 20 });
  const fv = stub.fakeWebviewView();
  fine.resolveWebviewView(fv);
  fine.onMessage({ type: "ready", size: "" });
  assert.strictEqual(fine.timer, null);
  // only time on screen counts: hidden before it loaded is not a failure, and
  // shown again is a new page load that is watched again
  mode._reset();
  stub.config.accountsStyle = "auto";
  await mode.apply();
  fv.setVisible(false);
  assert.strictEqual(fine.timer, null);
  const hidden = new web.AccountsView({ readyWaitMs: 20 });
  const hv = stub.fakeWebviewView();
  hidden.resolveWebviewView(hv);
  hv.setVisible(false);
  await stub.sleep(60);
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], false, "hidden: no fallback");
  hv.setVisible(true);
  await stub.sleep(60);
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], true, "blank again after showing it: the list");
  // a "ready" already waiting when the timer fires wins
  mode._reset();
  await mode.apply();
  const late = new web.AccountsView({ readyWaitMs: 5 });
  late.resolveWebviewView(stub.fakeWebviewView());
  const until = Date.now() + 30;
  while (Date.now() < until); // the extension host is busy past the deadline
  setImmediate(() => late.onMessage({ type: "ready", size: "" }));
  await stub.sleep(30);
  assert.strictEqual(stub.contexts["cswapStatus.accountsList"], false, "late ready: still the cards");
  mode._reset();
  await mode.apply();
  delete stub.config.accountsStyle;
  stub.configUpdates.length = 0;
  web.setLogDir(null);
  require("fs").rmSync(logDir, { recursive: true, force: true });
  for (let i = 0; i < 50 && !statusItems[0].text; i++) await stub.sleep(50);
  const item = statusItems[0];

  // compact status bar
  assert.strictEqual(item.text, "$(account) #3 5h 34%");
  // the window closest to its line; the line itself only when it is the account's own
  const cu = require("../lib/format").compactUsage;
  assert.strictEqual(cu({ usage5h: 34, max5h: 40, max5hSource: "own", usage7d: 82, max7d: 95, max7dSource: "own" }), "5h 34/40%");
  assert.strictEqual(cu({ usage5h: 10, max5h: 98, max5hSource: "threshold", usage7d: 90, max7d: 95, max7dSource: "own" }), "7d 90/95%");
  assert.strictEqual(cu({ usage5h: 10, max5h: 98, max5hSource: "threshold", usage7d: 50, max7d: 98, max7dSource: "threshold" }), "7d 50%");
  assert.strictEqual(cu({ usage5h: null, max5h: 98, usage7d: 50, max7d: 98 }), "7d 50%");
  assert.strictEqual(cu({ usage5h: null, usage7d: null }), "5h ?%");
  assert.strictEqual(item.command, "cswapStatus.openPanel");
  assert.match(item.tooltip.value, /\| ▶ 3: c@x \| 34\/98% \| 20\/98% \| 当前 \|/);
  assert.match(item.tooltip.value, /\| 6: f@x \| 10\/98% \| 10\/98% \| 到上限（opus） \|/);
  assert.strictEqual(item.backgroundColor, undefined);
  stub.config.statusBar = "full";
  await T.refresh();
  assert.match(item.text, /│ \$\(account\) c@x 5h 34\/98% · 7d 20\/98%$/);
  stub.config.statusBar = "off";
  await T.refresh();
  assert.ok(!item.visible);
  stub.config.statusBar = "compact";
  await T.refresh();
  assert.ok(item.visible);

  // 账号 view: the machine and the switch threshold, then one card per
  // account: number and name, tags only for what is unusual (the reserve
  // account is the 主账号), a 切换 button, a ring per window
  assert.match(text(page()), /^本机 .* 阈值 98%/);
  let c = cards();
  assert.deepStrictEqual(Object.keys(c), ["1", "2", "3", "4", "5", "6"]);
  assert.strictEqual(c["3"].text, "#3 c@x 当前 5h 34% 7d 20%");
  assert.deepStrictEqual(arcs(c["3"]), [34, 20]);
  assert.strictEqual(c["1"].text, "#1 a@x 主账号 切换 5h 2% 7d 26%");
  assert.strictEqual(c["2"].text, "#2 b@x 已停用 切换 5h 29% 7d 23%");
  // at a limit: no 切换 button (it would be switched back at once)
  assert.strictEqual(c["6"].text, "#6 f@x 到上限（opus） 5h 10% 7d 10%");
  assert.ok(c["2"].off && !c["1"].off);
  // red at the account's own line, yellow within 15 points of it, a tick
  // where that line is
  assert.deepStrictEqual(levels(c["2"]), ["over", ""]); // 29 vs its own 5
  assert.deepStrictEqual(levels(c["4"]), ["near", ""]); // 95 vs 98
  assert.deepStrictEqual(levels(c["5"]), ["near", ""]); // 60 vs its own 70
  assert.deepStrictEqual(levels(c["3"]), ["", ""]);
  assert.deepStrictEqual(ticks(c["2"]), [5, 98]);
  assert.deepStrictEqual(ticks(c["5"]), [70, 98]);
  // the threshold link opens the 自动切换 threshold setting
  await view.send({ type: "threshold" });
  assert.deepStrictEqual(executed.pop(), ["cswapStatus.autoThreshold"]);
  assert.match(c["2"].html, /title="已停用\npersonal 5h limit"/);
  // right-click items per card
  assert.deepStrictEqual(menu(c["3"]), ["accountAlias", "accountLimits", "accountReserveOn", "accountDisable", "accountRelogin", "accountRemove"]);
  assert.deepStrictEqual(menu(c["1"]), ["switchAccount", "accountAlias", "accountLimits", "accountReserveOff", "accountDisable", "accountRelogin", "accountRemove"]);
  assert.deepStrictEqual(menu(c["2"]), ["switchAccount", "accountAlias", "accountLimits", "accountReserveOn", "accountEnable", "accountRelogin", "accountRemove"]);

  // the `state` field wins over the status text
  const withState = ACCOUNTS.map((r) => ({ ...r }));
  withState[2] = { ...withState[2], state: "limit", blockedWindow: "5h", status: "Active", usage5h: 99, switchable: true };
  withState[3] = { ...withState[3], state: "ready", switchable: true };
  withState[0] = { ...withState[0], state: "reserve", switchable: true };
  withState[1] = { ...withState[1], state: "disabled", switchable: false };
  withState[4] = { ...withState[4], state: "off-schedule", status: "Ready", switchable: true };
  withState[5] = { ...withState[5], state: "ready", status: "Ready", switchable: true };
  setRows(withState);
  await T.refresh();
  c = cards();
  assert.strictEqual(c["3"].text, "#3 c@x 当前 到上限 5h 99% 7d 20%");
  assert.deepStrictEqual(levels(c["3"]), ["over", ""]);
  assert.strictEqual(item.backgroundColor.id, "statusBarItem.warningBackground");
  assert.strictEqual(c["5"].text, "#5 e@x 不在时段 5h 60% 7d 10%");
  assert.strictEqual(c["6"].text, "#6 f@x 切换 5h 10% 7d 10%");
  // no 切换 (button or menu item) for rows cswap says it cannot switch to
  assert.deepStrictEqual(c["2"].ctx, {
    webviewSection: "account", accountNumber: "2", cswapKind: "unswitchable", cswapOn: false, cswapReserve: false, preventDefaultContextMenuItems: true,
  });
  assert.ok(!c["2"].html.includes('data-cmd="switch"'));
  assert.deepStrictEqual(menu(c["2"]), ["accountAlias", "accountLimits", "accountReserveOn", "accountEnable", "accountRelogin", "accountRemove"]);
  assert.deepStrictEqual(menu(c["4"]), ["switchAccount", "accountAlias", "accountLimits", "accountReserveOn", "accountDisable", "accountRelogin", "accountRemove"]);

  // the same accounts as a list (cswapStatus.accountsStyle list, or auto after a failed page),
  // with the same right-click items as the cards, and 切换 inline where the
  // card has its button
  const L = require("../lib/accountsList");
  const rows = new L.AccountsList().getChildren();
  assert.deepStrictEqual(rows[0].label, "阈值 98%");
  assert.deepStrictEqual(rows[0].command.command, "cswapStatus.autoThreshold");
  const byNum = Object.fromEntries(rows.slice(1).map((r) => [r.accountNumber, r]));
  // a narrow row: the name before the @ (unless two share it), the full one in the tooltip
  assert.strictEqual(byNum["3"].label, "#3 c");
  assert.match(byNum["3"].tooltip, /^c@x\n/);
  assert.strictEqual(L.accountItem({ number: "9", name: "c@y", usage5h: 1, usage7d: 2 }, 0, [{ name: "c@x" }, { name: "c@y" }]).label, "#9 c@y");
  // the shared threshold is on the row above; only an account's own line after the slash
  const own = L.accountItem({ number: "9", name: "z@x", usage5h: 27, max5h: 40, max5hSource: "own", usage7d: 80, max7d: 98, max7dSource: "threshold" }, 0, []);
  assert.strictEqual(own.description, "5h 27/40% · 7d 80%");
  // tags first (a narrow side bar cuts the end)
  assert.strictEqual(byNum["3"].description, "当前 · 到上限 · 5h 99/98% · 7d 20/98%");
  assert.strictEqual(byNum["5"].description, "不在时段 · 5h 60/70% · 7d 10/98%");
  // the tooltip has when each window resets, and the card's notes
  const tipRow = L.accountItem(
    { number: "9", name: "z@x", usage5h: 34, max5h: 40, usage7d: 82, max7d: 95, resetsAt5h: 1000 + 7200, resetsAt7d: 900,
      usageAgeSeconds: 7200, quarantined: "invalid_grant" }, 1000, []);
  assert.match(tipRow.tooltip, /^5h 34 \/ 40% · 2 小时后重置（\d\d:\d\d）$/m);
  assert.match(tipRow.tooltip, /^7d 82 \/ 95%$/m, "a reset already past is left out");
  assert.match(tipRow.tooltip, /用量是 2 小时前的/);
  assert.match(tipRow.tooltip, /自动切换已隔离：invalid_grant/);
  // no icon folder: theme icons; with one: a light and a dark ring picture
  assert.strictEqual(byNum["2"].iconPath.id, "circle-slash");
  const iconDir = path.join(tmp, "icons");
  L.setIconDir(iconDir);
  const withIcons = Object.fromEntries(new L.AccountsList().getChildren().slice(1).map((r) => [r.accountNumber, r]));
  assert.match(withIcons["3"].iconPath.light, /ring-[0-9a-f]+\.svg$/);
  const darkSvg = fs.readFileSync(withIcons["3"].iconPath.dark, "utf8");
  assert.match(darkSvg, /stroke="#f14c4c"[^>]*stroke-dasharray="99.0 100"/, "5h at its line: red");
  assert.match(fs.readFileSync(withIcons["2"].iconPath.light, "utf8"), /opacity="0.5"/, "disabled: faded");
  assert.notStrictEqual(withIcons["3"].iconPath.dark, withIcons["4"].iconPath.dark);
  // a half-written icon is written again; old icons and leftovers are pruned
  const ring = require("../lib/ringIcon");
  fs.writeFileSync(withIcons["3"].iconPath.dark, "<svg");
  new L.AccountsList().getChildren();
  assert.strictEqual(fs.readFileSync(withIcons["3"].iconPath.dark, "utf8"), darkSvg);
  const leftover = path.join(iconDir, "ring-0123456789abcdef.svg.99.tmp");
  fs.writeFileSync(leftover, "x");
  const old = new Date(Date.now() - 3 * 86400e3);
  fs.utimesSync(withIcons["2"].iconPath.light, old, old);
  fs.utimesSync(leftover, old, old);
  ring.prune(iconDir);
  assert.ok(!fs.existsSync(withIcons["2"].iconPath.light) && !fs.existsSync(leftover));
  assert.ok(fs.existsSync(withIcons["3"].iconPath.dark));
  assert.ok(!fs.readdirSync(iconDir).some((n) => n.endsWith(".tmp")));
  L.setIconDir(null);
  function listWhen(expr, item) {
    return expr.split("&&").every((part) => {
      const p = part.trim();
      let m;
      if ((m = p.match(/^view == (\S+)$/))) return m[1] === "cswapStatus.accountsList";
      if ((m = p.match(/^(!?)\(?viewItem =~ \/(.*)\/\)?$/))) return new RegExp(m[2]).test(item.contextValue) !== (m[1] === "!");
      throw new Error(`when-clause not understood: ${p}`);
    });
  }
  const listMenu = (item, inline) =>
    pkg.contributes.menus["view/item/context"]
      .filter((m) => m.when.includes("cswapStatus.accountsList") && (m.group === "inline") === inline && listWhen(m.when, item))
      .map((m) => m.command.replace("cswapStatus.", ""));
  for (const n of Object.keys(c)) {
    assert.deepStrictEqual(listMenu(byNum[n], false), menu(c[n]), `#${n} right-click`);
    const buttons = [...c[n].html.matchAll(/data-cmd="(switch|relogin)"/g)].map((m) => (m[1] === "switch" ? "switchAccount" : "accountRelogin"));
    assert.deepStrictEqual(listMenu(byNum[n], true), buttons, `#${n} inline button`);
  }
  // the two 账号 views are never shown together, and share the title buttons
  const views = pkg.contributes.views.cswap;
  assert.deepStrictEqual(views.slice(0, 2).map((v) => [v.id, v.when]), [
    ["cswapStatus.accounts", "!cswapStatus.accountsList"],
    ["cswapStatus.accountsList", "cswapStatus.accountsList"],
  ]);
  for (const t of pkg.contributes.menus["view/title"].filter((m) => m.when.includes("cswapStatus.accounts"))) {
    assert.ok(t.when.includes("cswapStatus.accountsList"), t.command);
  }
  // a workspace's .vscode/settings.json cannot choose the program the side bar runs
  assert.strictEqual(pkg.contributes.configuration.properties["cswapStatus.path"].scope, "machine");

  // 切换 on a fine account: no question, runs cswap switch in the background
  const infos = shown.info.length;
  await view.send({ type: "switch", n: "1" });
  assert.deepStrictEqual(switchCalls(), ["switch 1"]);
  // confirmed in the status bar only: no terminal, no pop-up
  assert.strictEqual(shown.status.pop(), "$(check) 已切换到 #1，下一条消息起生效，不用重启 Claude");
  assert.strictEqual(shown.info.length, infos);
  assert.strictEqual(terminals.length, 0);
  // the refresh after it shows cswap's record of that manual switch: no
  // second pop-up for it, but an automatic switch still gets one
  const switched = (ls) => cswap.set({ ...OK, "policy list": { out: { schemaVersion: 1, accounts: withState, autoswitch: { threshold: 98, lastSwitch: ls } } } });
  switched({ at: 1000, from: "3", to: "1", trigger: "manual", auto: false });
  await T.refresh();
  switched({ at: 2000, from: "3", to: "1", trigger: "manual", auto: false });
  await T.refresh();
  assert.strictEqual(shown.info.length, infos);
  switched({ at: 3000, from: "1", to: "4", trigger: "policy-limit", auto: true });
  await T.refresh();
  assert.strictEqual(shown.info.pop(), "cswap 自动切换 #1 → #4（个人上限）");
  setRows(withState);
  await T.refresh();
  // 切换 to an off-schedule account: confirm first; dismissing does nothing
  answers.warn.push(undefined);
  await view.send({ type: "switch", n: "5" });
  assert.strictEqual(shown.warn.pop(), "自动切换会在约 1 分钟内切回，因为 #5 不在它的使用时段。仍要切换？");
  assert.deepStrictEqual(switchCalls(), ["switch 1"]);
  answers.warn.push("仍要切换");
  await view.send({ type: "switch", n: "5" });
  assert.deepStrictEqual(switchCalls(), ["switch 1", "switch 5"]);
  // old cswap (no state field), from the right-click menu: VS Code passes
  // the card's data-vscode-context
  setRows(ACCOUNTS);
  await T.refresh();
  c = cards();
  answers.warn.push(undefined);
  await T.switchAccount({ webview: "cswapStatus.accounts", ...c["6"].ctx });
  assert.strictEqual(shown.warn.pop(), "自动切换会在约 1 分钟内切回，因为 #6 已到 opus 上限。仍要切换？");
  answers.warn.push(undefined);
  await T.switchAccount({ webview: "cswapStatus.accounts", ...c["2"].ctx });
  assert.strictEqual(shown.warn.pop(), "自动切换会在约 1 分钟内切回，因为 #2 已停用。仍要切换？");
  assert.deepStrictEqual(switchCalls(), ["switch 1", "switch 5"]);
  // a malformed account number never reaches cswap
  await view.send({ type: "switch", n: "1; rm -rf ~" });
  await T.switchAccount({ accountNumber: "1; rm -rf ~" });
  assert.deepStrictEqual(switchCalls(), ["switch 1", "switch 5"]);
  // a failed switch says why
  cswap.set({ ...OK, switch: { err: "Error: Account-9 not found\n", code: 1 }, "policy list": { out: { schemaVersion: 1, accounts: ACCOUNTS } } });
  await T.switchAccount({ accountNumber: "9" });
  assert.strictEqual(shown.error.pop(), "cswap switch 失败：Error: Account-9 not found");

  // cswap failures say what went wrong instead of "not found"
  cswap.set({ "policy list": { err: "Traceback\nRuntimeError: store locked\n", code: 1 } });
  await T.refresh();
  assert.strictEqual(T.accounts.state.error, "cswap policy list 失败：RuntimeError: store locked");
  assert.match(page(), /<div class="err">cswap policy list 失败：RuntimeError: store locked<\/div>/);
  assert.strictEqual(item.text, "$(warning) cswap?");
  cswap.set({ "policy list": { out: "{oops" } });
  await T.refresh();
  assert.match(T.accounts.state.error, /^cswap policy list 的输出无法解析：/);
  cswap.set({ "policy list": { err: "usage: cswap ...\ncswap: error: invalid choice: 'policy'\n", code: 2 } });
  await T.refresh();
  assert.strictEqual(T.accounts.state.error, "cswap policy list 失败：cswap: error: invalid choice: 'policy'");
  stub.config.path = path.join(tmp, "nope", "cswap");
  T.proc.resetBins();
  await T.refresh();
  assert.match(T.accounts.state.error, /^cswap 未找到/);
  stub.config.path = cswap.bin;
  T.proc.resetBins();
  // a hanging program is reported as a timeout
  const slow = fakeProgram(tmp, "slow");
  slow.set({ x: { sleep: 3, out: "late" } });
  const r = await T.proc.exec(slow.bin, ["x"], { timeout: 300 });
  assert.ok(r.timedOut);
  assert.strictEqual(r.err, "超时");
  // stdin input reaches the program
  slow.set({ y: { out: "ok" } });
  await T.proc.exec(slow.bin, ["y"], { input: "hello" });
  assert.strictEqual(slow.calls().pop().stdin, "hello");

  // satellite: a plan that is not JSON is an error, and the server is not saved
  setRows(ACCOUNTS);
  await T.refresh();
  sat.set({ plan: { out: "Welcome to srv!" } });
  answers.input.push("srv");
  await T.satellite.addAccounts();
  assert.match(shown.error.pop(), /^cswap-satellite plan 的输出无法解析/);
  assert.strictEqual(stub.configUpdates.length, 0);
  const planCall = sat.calls().pop().argv;
  assert.deepStrictEqual(planCall, ["plan", "--server", "srv", "--cswap", cswap.bin]);
  // a good plan: the server is remembered, tokens go on stdin
  sat.set({
    plan: { out: { accounts: [{ number: 1, action: "skip", reason: "already there" }, { number: 2, email: "b@x", action: "add" }] } },
    add: { out: "账号 2 已添加" },
  });
  answers.input.push("srv", "sk-ant-oat01-" + "a".repeat(30));
  await T.satellite.addAccounts();
  assert.deepStrictEqual(stub.configUpdates.pop().slice(0, 2), ["satelliteServer", "srv"]);
  const add = sat.calls().pop();
  assert.deepStrictEqual(add.argv.slice(0, 5), ["add", "--account", "2", "--server", "srv"]);
  assert.strictEqual(add.stdin, "sk-ant-oat01-" + "a".repeat(30) + "\n");
  assert.strictEqual(shown.info.pop(), "跳过账号 1：already there；账号 2 已添加");
  // a failed usage sync shows in the tree
  sat.set({ "sync-usage": { err: "ssh: connect to host srv: refused\n", code: 255 } });
  T.satellite._sync.last = 0;
  await T.satellite.maybeSync();
  assert.strictEqual(T.satellite.syncNote(), "从 srv 同步用量失败：ssh: connect to host srv: refused");
  assert.strictEqual(T.satellite.maybeSync(), null); // not again within 5 minutes
  await T.refresh();
  assert.match(page(), /<div class="warnline">从 srv 同步用量失败：ssh: connect to host srv: refused<\/div>$/);

  // names are escaped; reset times; failed usage reads say how old the numbers are
  const now = Date.now() / 1000;
  setRows([
    { number: "7", name: '<img src=x onerror="alert(1)">', active: false, enabled: true, usage5h: 6, usage7d: 34, max5h: 98, max7d: 98,
      state: "ready", switchable: true, resetsAt5h: now + 7210, resetsAt7d: now + 2 * 86400 + 100 },
    { number: "8", name: "h@x", active: false, enabled: false, usage5h: 4, usage7d: 34, max5h: 98, max7d: 98,
      state: "disabled", switchable: false, lastError: "http-400", usageAgeSeconds: 2743 },
  ]);
  await T.refresh();
  c = cards();
  assert.ok(!page().includes("<img"));
  assert.strictEqual(c["7"].text, '#7 <img src=x onerror="alert(1)"> 切换 5h 6% 2时 7d 34% 2天');
  assert.match(c["7"].html, /title="5 小时：已用 6%\n用到 98% 切换\n2 小时后重置（\d\d-\d\d \d\d:\d\d）"/);
  // usage cannot be read: its card offers 重新登录
  assert.strictEqual(c["8"].text, "#8 h@x 已停用 重新登录 读取用量失败（http-400） 用量是 46 分钟前的 5h 4% 7d 34%");
  assert.match(c["8"].html, /<button data-cmd="relogin" data-n="8">/);
  // at its 5h limit: when it can be used again instead of a 切换 button
  setRows([{ number: "9", name: "i@x", active: false, enabled: true, usage5h: 99, usage7d: 30, max5h: 98, max7d: 98, max5hSource: "own",
    state: "limit", blockedWindow: "5h", switchable: true, resetsAt5h: now + 4520, resetsAt7d: now + 86400 * 3 }]);
  await T.refresh();
  c = cards();
  assert.strictEqual(c["9"].text, "#9 i@x 到上限 1时15分后可用 5h 99% 1时15分 7d 30% 3天");
  assert.match(c["9"].html, /用到 98% 切换（这个账号自己的上限）/);
  // no accounts: the page's button opens 添加账号
  setRows([]);
  await T.refresh();
  assert.match(page(), /还没有账号/);
  answers.pick.push(undefined);
  await view.send({ type: "add" });
  assert.strictEqual(answers.pick.length, 0);
  // a page that (re)loads asks for the latest body
  const posts = view.webview.posted.length;
  await view.send({ type: "ready" });
  assert.strictEqual(view.webview.posted.length, posts + 1);
  // the page runs only its own nonce'd script and has no inline styles
  const nonce = view.webview.html.match(/script-src 'nonce-([0-9a-f]+)'/)[1];
  assert.ok(view.webview.html.includes(`<script nonce="${nonce}">`));
  assert.ok(!/ style=/.test(view.webview.html + page()));

  // tmux: claude is quoted for the shell inside tmux
  assert.deepStrictEqual(T.tmux.tmuxArgs("/opt/it's/claude", "cr-w", "/w", true), [
    "new-session", "-A", "-s", "cr-w", "-c", "/w", `'/opt/it'\\''s/claude' --continue; exec "\${SHELL:-/bin/sh}"`,
  ]);

  ext.deactivate();
  console.log("accounts tests: OK");
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
