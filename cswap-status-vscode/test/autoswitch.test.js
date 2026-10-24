// The 自动切换 view (lib/autoswitch.js) against a fake cswap whose
// `policy list --json` carries the `autoswitch` block of the suite contract.
//   node test/autoswitch.test.js
"use strict";

const assert = require("assert");
const os = require("os");
const path = require("path");
const stub = require("./vscode-stub");
const { tmpDir, fakeProgram, hermetic } = require("./fakes");

const tmp = tmpDir("auto");
const bin = hermetic(tmp);
const cswap = fakeProgram(tmp, "cswap");
Object.assign(stub.config, { path: cswap.bin, showApi: false, refreshSeconds: 60, keepWorkflowsEnabled: false, statusBar: "compact" });
const { shown, answers, statusItems, contexts } = stub;

const ext = require("../extension.js");
const T = ext._test;

const now = () => Date.now() / 1000;
const ROWS = [
  { number: "1", name: "a@x", active: false, enabled: true, usage5h: 26, usage7d: 20, max5h: 98, max7d: 98, status: "Reserve", state: "reserve", reserve: true, switchable: true },
  { number: "2", name: "b@x", active: false, enabled: false, usage5h: 31, usage7d: 23, max5h: 5, max7d: 98, status: "Disabled", state: "disabled", switchable: false },
  {
    number: "3", name: "work", active: true, enabled: true, usage5h: 47, usage7d: 22, max5h: 98, max7d: 98, status: "Active", state: "active",
    max5hSource: "own", max7dSource: "threshold", resetsAt5h: null, resetsAt7d: null, usageAgeSeconds: 30, switchable: true,
  },
  { number: "4", name: "d@x", active: false, enabled: true, usage5h: 2, usage7d: 26, max5h: 98, max7d: 98, status: "Usage unknown", state: "unknown", quarantined: "invalid_grant", lastError: "http-400", switchable: true },
];

function block(over) {
  const t = now();
  return {
    threshold: 98.0, weeklyThreshold: null, intervalSeconds: 60, cooldownSeconds: 300, hysteresisPct: 10.0, strategy: "best",
    pausedUntil: null, settingsMtime: t - 5000,
    lastSwitch: { at: t - 120, from: "2", to: "3", trigger: "policy-limit", auto: true },
    cooldownUntil: t + 180,
    nextCandidate: { number: "1", reserve: true, priority: 100, scheduledNow: false, headroomPct: 74.0, why: "only other enabled account; reserve" },
    excluded: { "2": "disabled", "4": "usage unknown (http-400)" },
    service: {
      schemaVersion: 1, pid: process.pid, startedAt: t - 3600, lastTickAt: t - 20, nextTickAt: t + 40,
      outcome: "no-action", trigger: null, reason: "below-threshold", detail: "#3 5h 47% / 98%, 7d 22% / 98%", active: "3", paused: false,
      settings: { threshold: 98.0, intervalSeconds: 60, cooldownSeconds: 300, hysteresisPct: 10.0, strategy: "best" }, stale: false,
    },
    history: [
      { at: t - 120, from: "2", to: "3", trigger: "policy-limit", auto: true },
      { at: t - 600, from: "3", to: "2", trigger: "manual", auto: false },
    ],
    ...(over || {}),
  };
}
const setPolicy = (rows, auto) =>
  cswap.set({
    "policy list": { out: { schemaVersion: 1, accounts: rows, ...(auto === undefined ? {} : { autoswitch: auto }) } },
    auto: { out: "ok" },
  });
const top = () => new T.AutoTree().getChildren();
const labels = () => top().map((i) => i.label);
const find = (prefix) => top().find((i) => String(i.label).startsWith(prefix));

(async () => {
  assert.deepStrictEqual(labels(), ["读取中…"]);

  // ---- running service with everything in the block
  setPolicy(ROWS, block());
  ext.activate({ extensionPath: path.join(__dirname, ".."), subscriptions: [] });
  for (let i = 0; i < 50 && !statusItems[0].text; i++) await stub.sleep(50);
  const item = statusItems[0];
  let l = labels();
  assert.strictEqual(l[0], "自动切换 运行中");
  assert.strictEqual(l[1], "切换阈值 98%");
  assert.strictEqual(l[2], "当前 #3：不切换");
  assert.strictEqual(l[3], "#3 距离触发");
  assert.strictEqual(l[4], "下一个 #1（主账号）");
  assert.match(l[5], /^冷却中 还剩 [23]:\d\d$/);
  assert.strictEqual(l[6], "上次 #2 → #3 · 个人上限");
  assert.strictEqual(l[7], "最近切换（2 次）");
  assert.strictEqual(l[8], "#4 已隔离 invalid_grant");
  assert.strictEqual(l.length, 9);
  const svc = find("自动切换");
  assert.strictEqual(svc.iconPath.id, "pulse");
  assert.strictEqual(svc.contextValue, "cswapAuto.pausable");
  assert.match(svc.description, /^自 \d\d:\d\d · 每 60s$/);
  const decision = find("当前");
  assert.strictEqual(decision.iconPath.id, "check");
  assert.match(decision.description, /^低于阈值 · #3 5h 47% \/ 98%, 7d 22% \/ 98% · \d+ 秒前$/);
  const dist = find("#3 距离触发");
  assert.strictEqual(dist.description, "5h 47→98 · 7d 22→98");
  assert.strictEqual(dist.iconPath.color.id, "charts.green");
  assert.match(dist.tooltip.value, /5h `█████░░░░░` 47% \/ 98%（个人上限） · 还差 51 点/);
  assert.match(dist.tooltip.value, /7d .*（全局阈值）/);
  const next = find("下一个");
  assert.strictEqual(next.description, "预计 · 余量 74%");
  assert.strictEqual(next.iconPath.color.id, "charts.yellow");
  assert.match(next.tooltip.value, /如果 #3 现在到上限，会切到 \*\*#1\*\*/);
  const cands = new T.AutoTree().getChildren(next);
  assert.deepStrictEqual(cands.map((c) => [c.label, c.iconPath.id, c.description]), [
    ["#1", "arrow-right", "下一个 · 余量 74%"],
    ["#2", "circle-slash", "已停用"],
    ["#4", "circle-slash", "用量未知 (http-400)"],
  ]);
  const hist = new T.AutoTree().getChildren(find("最近切换"));
  assert.deepStrictEqual(hist.map((h) => [h.iconPath.id, h.description]), [["zap", "个人上限"], ["person", "手动"]]);
  assert.match(hist[0].label, /^\d\d-\d\d \d\d:\d\d #2 → #3$/);
  assert.match(find("上次").tooltip.value, /个人上限（不是 Anthropic 的限额）/);
  assert.strictEqual(contexts["cswapAuto.state"], "running");
  assert.strictEqual(contexts["cswapAuto.canPause"], true);
  assert.strictEqual(contexts["cswapAuto.paused"], false);
  assert.strictEqual(item.text, "$(account) #3 5h 47%");
  // the first refresh never notifies, even with a lastSwitch present
  assert.strictEqual(shown.info.length, 0);
  // no extra cswap process per tick: one policy list per refresh, nothing else
  assert.deepStrictEqual(cswap.calls().map((c) => c.argv.join(" ")), ["policy list --json"]);

  // ---- a new switch is announced once
  setPolicy(ROWS, block({ lastSwitch: { at: now() - 5, from: "3", to: "1", trigger: "policy-limit", auto: true } }));
  await T.refresh();
  await T.refresh();
  assert.deepStrictEqual(shown.info, ["cswap 自动切换 #3 → #1（个人上限）"]);
  stub.config.notifyOnSwitch = false;
  setPolicy(ROWS, block({ lastSwitch: { at: now() - 1, from: "1", to: "3", trigger: "reserve-return", auto: true } }));
  await T.refresh();
  assert.strictEqual(shown.info.length, 1);
  stub.config.notifyOnSwitch = true;

  // ---- near and over the line; no candidate
  const near = ROWS.map((r) => (r.number === "3" ? { ...r, usage5h: 90 } : r));
  setPolicy(near, block({ nextCandidate: null, cooldownUntil: null }));
  await T.refresh();
  assert.strictEqual(find("#3 距离触发").iconPath.color.id, "charts.yellow");
  const none = find("没有可切换的账号");
  assert.strictEqual(none.iconPath.color.id, "charts.red");
  assert.ok(!find("冷却中"));
  const over = ROWS.map((r) => (r.number === "3" ? { ...r, usage5h: 99, state: "limit", blockedWindow: "5h", resetsAt5h: now() + 4520 } : r));
  const exhausted = block();
  exhausted.service = { ...exhausted.service, reason: "policy-exhausted" };
  setPolicy(over, exhausted);
  await T.refresh();
  // every account at its limit: one warning naming the first one back
  // (#2 is disabled, #4 quarantined, #1 under its line but excluded by cswap)
  const warns = shown.warn.length;
  assert.match(shown.warn[warns - 1], /^所有账号都到上限了，自动切换没有账号可切。最早 #3 在 \d\d:\d\d 恢复（1时15分后）。$/);
  await T.refresh();
  assert.strictEqual(shown.warn.length, warns);
  assert.strictEqual(find("#3 距离触发").iconPath.color.id, "charts.red");
  assert.strictEqual(find("当前").label, "当前 #3：无法切换");
  assert.strictEqual(find("当前").iconPath.color.id, "charts.red");

  // ---- paused (until a time, then forever, then expired)
  const paused = block({ pausedUntil: now() + 7200 });
  paused.service = { ...paused.service, paused: true, reason: "paused" };
  setPolicy(ROWS, paused);
  await T.refresh();
  let s = find("自动切换");
  assert.strictEqual(s.label, "自动切换 已暂停");
  assert.strictEqual(s.contextValue, "cswapAuto.paused");
  assert.match(s.description, /^到 \d\d:\d\d（还剩 (1 小时 \d+ 分|2 小时)）$/);
  assert.strictEqual(find("当前").iconPath.id, "debug-pause");
  assert.strictEqual(item.text, "$(account) #3 5h 47% $(debug-pause)");
  assert.strictEqual(item.backgroundColor.id, "statusBarItem.warningBackground");
  assert.strictEqual(contexts["cswapAuto.paused"], true);
  assert.strictEqual(contexts["cswapAuto.canPause"], false);
  setPolicy(ROWS, block({ pausedUntil: "forever" }));
  await T.refresh();
  assert.strictEqual(find("自动切换").description, "一直暂停");
  setPolicy(ROWS, block({ pausedUntil: now() - 10 }));
  await T.refresh();
  assert.strictEqual(find("自动切换").label, "自动切换 运行中");
  assert.strictEqual(item.backgroundColor, undefined);

  // ---- stale (process alive but not ticking), stopped (process gone), no status file
  const stale = block();
  stale.service = { ...stale.service, lastTickAt: now() - 900, stale: true };
  setPolicy(ROWS, stale);
  await T.refresh();
  assert.strictEqual(find("自动切换").label, "自动切换 卡住了");
  assert.strictEqual(find("自动切换").iconPath.id, "error");
  assert.strictEqual(item.text, "$(account) #3 5h 47% $(error)");
  stale.service = { ...stale.service, pid: 2 ** 22 + 12345 };
  setPolicy(ROWS, stale);
  await T.refresh();
  assert.strictEqual(find("自动切换").label, "自动切换 已停止");
  assert.match(find("自动切换").description, /^上次检查 15 分钟前$/);
  assert.strictEqual(contexts["cswapAuto.state"], "stopped");
  setPolicy(ROWS, block({ service: null }));
  await T.refresh();
  assert.strictEqual(find("自动切换").label, "自动切换 没有运行");
  assert.ok(!find("当前"));
  assert.strictEqual(item.text, "$(account) #3 5h 47%");

  // ---- settings changed after the service started
  const changedSettings = block();
  changedSettings.service = { ...changedSettings.service, settings: { ...changedSettings.service.settings, threshold: 90 } };
  setPolicy(ROWS, changedSettings);
  await T.refresh();
  assert.match(find("自动切换").description, /设置已改，需重启$/);

  // ---- the threshold row: click to change it; accounts with their own
  // limits ignore it, so the row and the confirmation name them
  const cfgCalls = () => cswap.calls().map((c) => c.argv).filter((a) => a[0] === "config" || (a[0] === "policy" && a[1] === "set"));
  const withCfg = (auto) =>
    cswap.set({ "policy list": { out: { schemaVersion: 1, accounts: ROWS, autoswitch: auto } }, "config set": { out: "" }, "config unset": { out: "" }, "policy set": { out: "" } });
  withCfg(block());
  await T.refresh();
  const rule = find("切换阈值");
  assert.strictEqual(rule.description, "1 个账号用自己的上限");
  assert.strictEqual(rule.command.command, "cswapStatus.autoThreshold");
  assert.strictEqual(rule.contextValue, "cswapAuto.threshold");
  assert.match(rule.tooltip.value, /#3 5h 98% · 7d 98%/);
  answers.input.push(undefined); // cancelled: nothing runs
  await T.autoswitch.setThreshold();
  answers.input.push("90", "");
  await T.autoswitch.setThreshold();
  assert.strictEqual(shown.info.pop(), "自动切换阈值 90%。但 #3（5h 98% · 7d 98%）设了自己的上限，按自己的上限切换，不看这个阈值。");
  answers.input.push("90", "95");
  answers.info.push("都改为跟随阈值");
  await T.autoswitch.setThreshold();
  assert.deepStrictEqual(cfgCalls(), [
    ["config", "set", "autoswitch.threshold", "90"],
    ["config", "set", "autoswitch.threshold", "90"],
    ["config", "set", "autoswitch.weeklyThreshold", "95"],
    ["policy", "set", "3", "--max-5h", "default", "--max-7d", "default"],
  ]);
  withCfg(block({ weeklyThreshold: 95 }));
  await T.refresh();
  assert.strictEqual(find("切换阈值").label, "切换阈值 98% · 7d 95%");
  answers.input.push("98", "");
  await T.autoswitch.setThreshold();
  assert.deepStrictEqual(cfgCalls().slice(-2), [["config", "set", "autoswitch.threshold", "98"], ["config", "unset", "autoswitch.weeklyThreshold"]]);

  // ---- pause / resume argv, after a confirm that names the host
  setPolicy(ROWS, block());
  await T.refresh();
  answers.pick.push((items) => items.find((i) => i.label === "2 小时"));
  answers.warn.push("暂停");
  await T.autoswitch.pause();
  assert.match(shown.warn.pop(), new RegExp(`^在 本机 ${os.hostname().replace(/\.local$/, "")} 上暂停 cswap 自动切换（2 小时）？`));
  assert.deepStrictEqual(cswap.calls().map((c) => c.argv).filter((a) => a[0] === "auto"), [["auto", "--pause", "2h"]]);
  answers.pick.push((items) => items.find((i) => i.label === "今天剩下的时间"));
  answers.warn.push(undefined); // dismissed: nothing runs
  await T.autoswitch.pause();
  answers.pick.push((items) => items.find((i) => i.label === "一直暂停"));
  answers.warn.push("暂停");
  await T.autoswitch.pause();
  answers.warn.push("恢复");
  await T.autoswitch.resume();
  assert.deepStrictEqual(cswap.calls().map((c) => c.argv).filter((a) => a[0] === "auto"), [
    ["auto", "--pause", "2h"],
    ["auto", "--pause", "forever"],
    ["auto", "--resume"],
  ]);
  assert.match(`${T.autoswitch.minutesLeftToday()}m`, /^\d+m$/);
  cswap.set({ "policy list": { out: { schemaVersion: 1, accounts: ROWS, autoswitch: block() } }, auto: { err: "boom\n", code: 1 } });
  answers.warn.push("恢复");
  await T.autoswitch.resume();
  assert.strictEqual(shown.error.pop(), "cswap auto --resume 失败：boom");

  // ---- an older cswap without the block: systemctl is-active only
  const systemctl = fakeProgram(bin, "systemctl");
  systemctl.set({ "--user is-active": { out: "active" } });
  setPolicy(ROWS.map(({ state, switchable, quarantined, ...r }) => r));
  await T.refresh();
  assert.deepStrictEqual(labels(), ["自动切换 运行中", "cswap 版本太旧，没有自动切换状态"]);
  assert.deepStrictEqual(systemctl.calls().pop().argv, ["--user", "is-active", "cswap-auto.service"]);
  assert.strictEqual(contexts["cswapAuto.canPause"], false);
  systemctl.set({ "--user is-active": { out: "inactive", code: 3 } });
  await T.refresh();
  assert.strictEqual(labels()[0], "自动切换 已停止");
  assert.strictEqual(item.text, "$(account) #3 5h 47% $(error)");
  // an unknown unit also prints "inactive", but exits 4: not installed, no $(error)
  systemctl.set({ "--user is-active": { out: "inactive", code: 4 } });
  await T.refresh();
  assert.strictEqual(labels()[0], "自动切换 本机未安装");
  assert.strictEqual(item.text, "$(account) #3 5h 47%");
  require("fs").unlinkSync(systemctl.bin);
  await T.refresh();
  assert.strictEqual(labels()[0], "自动切换 本机未安装");
  assert.strictEqual(item.text, "$(account) #3 5h 47%");
  const autoCalls = cswap.calls().filter((c) => c.argv[0] === "auto").length;
  await T.autoswitch.pause();
  assert.match(shown.warn.pop(), /版本太旧/);
  assert.strictEqual(cswap.calls().filter((c) => c.argv[0] === "auto").length, autoCalls);

  // ---- cswap fails: one row with the reason
  cswap.set({ "policy list": { out: "not json at all" } });
  await T.refresh();
  assert.deepStrictEqual(labels(), ["cswap policy list 没有输出 JSON（这个 cswap 可能不含 policy 命令）"]);

  ext.deactivate();
  console.log("autoswitch tests: OK");
  // excluded reasons exactly as cswap writes them come out in Chinese
  const zh = T.autoswitch.excludedZh;
  assert.strictEqual(zh("policy limit (personal 5h limit: 99% / 98%)"), "到个人上限 (个人 5h 上限 99% / 98%)");
  assert.strictEqual(zh("over threshold (threshold 7d: 99% / 98%)"), "到阈值 (阈值 7d 99% / 98%)");
  assert.strictEqual(zh("no stored backup"), "没有备份");
  assert.strictEqual(zh("api-key account"), "API key 账号");
  assert.strictEqual(zh("no headroom"), "没有余量");

  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
