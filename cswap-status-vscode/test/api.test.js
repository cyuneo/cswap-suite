// The API worker part (lib/api.js, lib/apiView.js) against the stub `vscode`
// module and a fake `api-worker`. No VS Code needed:
//   node test/api.test.js        (any Node 16+, e.g. VS Code Server's bundled node)
"use strict";

const assert = require("assert");
const path = require("path");
const stub = require("./vscode-stub");
const { tmpDir, fakeProgram, hermetic } = require("./fakes");

const tmp = tmpDir("api");
hermetic(tmp);
const worker = fakeProgram(tmp, "api-worker");
const cswap = fakeProgram(tmp, "cswap");
cswap.set({ "policy list": { out: { schemaVersion: 1, accounts: [] } } });
Object.assign(stub.config, { apiWorkerPath: worker.bin, path: cswap.bin, refreshSeconds: 60, keepWorkflowsEnabled: false, statusBar: "compact" });
stub.vscode.workspace.workspaceFolders = [{ name: "w", uri: { fsPath: tmp } }];
const { shown, answers, statusItems } = stub;

const ext = require("../extension.js");
const T = ext._test;

const LIST = {
  tasks: [
    { id: "t11111111", kind: "api", title: "write tests", state: "running", key: "jj", budgetUsd: 2, createdAt: 2 },
    { id: "t22222222", kind: "cloud", title: "cloud job", state: "running", account: "3", sessionUrl: "https://claude.ai/code/s", createdAt: 1 },
    { id: "t33333333", kind: "api", title: "old", state: "done", key: "jj", costUsd: 0.42, createdAt: 0 },
  ],
  keys: [
    { name: "jj", spentUsd: 10, reservedUsd: 2, monthlyBudgetUsd: 200, leftUsd: 188 },
    { name: "gmail", spentUsd: 95, reservedUsd: 0, monthlyBudgetUsd: 100, leftUsd: 5 },
  ],
};
const listCalls = () => worker.calls().filter((c) => c.argv[0] === "list").map((c) => c.argv);

(async () => {
  worker.set({ list: { out: LIST } });
  ext.activate({ extensionPath: path.join(__dirname, ".."), subscriptions: [] });
  for (let i = 0; i < 50 && !statusItems[1].text; i++) await stub.sleep(50);
  const apiItem = statusItems[1];

  // compact status bar (the default): short text, a click opens the side bar
  assert.strictEqual(apiItem.text, "$(sync~spin) $193 2");
  assert.strictEqual(apiItem.command, "cswapStatus.openPanel");
  // the worker is asked not to list cswap accounts (the extension never uses them)
  assert.deepStrictEqual(listCalls()[0], ["list", "--limit", "30", "--no-accounts"]);
  assert.strictEqual(stub.contexts["cswapStatus.apiAvailable"], true);

  // full mode: the long text of earlier versions
  stub.config.statusBar = "full";
  await T.refreshApi();
  assert.match(apiItem.text, /API 剩 \$193\.00 · 2 个运行中/);
  assert.match(apiItem.tooltip.value, /\| gmail \| \$95\.00 \| \$0\.00 \| \$100\.00 \| \$5\.00 \|/);
  assert.ok(apiItem.visible);
  stub.config.statusBar = "off";
  await T.refreshApi();
  assert.ok(!apiItem.visible);
  stub.config.statusBar = "compact";
  await T.refreshApi();
  assert.ok(apiItem.visible);

  // tree: groups, keys with colour by use, running vs recent, Chinese states
  const tree = new T.ApiTree();
  const groups = tree.getChildren();
  assert.deepStrictEqual(groups.map((g) => g.label), ["本月预算（2 个 key）", "进行中（2）", "最近的任务（1）"]);
  const keys = tree.getChildren(groups[0]);
  assert.strictEqual(keys[1].iconPath.color.id, "charts.red");
  assert.match(keys[0].description, /剩 \$188\.00 \/ \$200\.00/);
  const running = tree.getChildren(groups[1]);
  assert.deepStrictEqual(running.map((t) => t.contextValue), ["apiTask.api.running", "apiTask.cloud.running"]);
  assert.strictEqual(running[0].description, "t11111111 · key jj · 运行中");
  // a cloud session still starting counts as in progress
  assert.strictEqual(T.format.isRunning({ state: "launching" }), true);
  assert.strictEqual(T.format.taskStateZh("launching"), "启动中");
  assert.strictEqual(tree.getChildren(groups[2])[0].contextValue, "apiTask.api.done");

  // a task that finishes is announced once
  worker.set({ list: { out: { ...LIST, tasks: [{ ...LIST.tasks[0], state: "done", costUsd: 1.5 }, LIST.tasks[1], LIST.tasks[2]] } } });
  await T.refreshApi();
  await T.refreshApi();
  assert.strictEqual(shown.info.filter((m) => m.includes("t11111111 完成")).length, 1);
  // `final` from the worker wins over the state name
  worker.set({ list: { out: { ...LIST, tasks: [{ ...LIST.tasks[1], state: "archived", final: true }] } } });
  await T.refreshApi();
  assert.strictEqual(shown.warn.filter((m) => m.includes("t22222222")).length, 1);

  // result document for a cloud task
  worker.set({
    result: {
      out: {
        id: "t22222222", kind: "cloud", title: "cloud job", state: "done", sessionUrl: "https://claude.ai/code/s",
        resultBranch: "claude/aw-t22222222", result: "All good.", commits: ["abc add"], diffStat: " a | 1 +",
        outputs: ["summary.md"], howToGet: "api-worker collect t22222222",
      },
    },
  });
  await T.openTaskResult("t22222222");
  const doc = shown.docs.pop();
  assert.match(doc, /# cloud job/);
  assert.match(doc, /- 状态：\*\*完成\*\*/);
  assert.match(doc, /## 输出文件\n\n- summary\.md/);
  assert.match(doc, /## 报告\n\nAll good\./);
  assert.deepStrictEqual(worker.calls().pop().argv, ["result", "t22222222"]);

  // a cloud session that cannot report back is not "in progress"; the user
  // marks it ended, and that is not announced back to them
  assert.strictEqual(T.format.taskStateZh("cloud"), "已交给云端（无法跟踪）");
  worker.set({ result: { out: { id: "t22222222", kind: "cloud", title: "cloud job", state: "cloud", final: false, result: null } } });
  await T.openTaskResult("t22222222");
  assert.doesNotMatch(shown.docs.pop(), /还没做完/);
  const untracked = { ...LIST.tasks[1], state: "cloud", final: false };
  worker.set({ list: { out: { ...LIST, tasks: [untracked] } }, close: { out: { id: "t22222222", state: "closed" } } });
  await T.refreshApi();
  const closeTree = new T.ApiTree();
  const [, , recentGroup] = closeTree.getChildren();
  const [untrackedRow] = closeTree.getChildren(recentGroup);
  assert.strictEqual(untrackedRow.contextValue, "apiTask.cloud.cloud");
  const warns = shown.warn.length;
  worker.set({ list: { out: { ...LIST, tasks: [{ ...untracked, state: "closed", final: true }] } }, close: { out: { id: "t22222222", state: "closed" } } });
  await T.apiView.closeTask(untrackedRow);
  assert.ok(worker.calls().some((c) => c.argv.join(" ") === "close t22222222"));
  assert.strictEqual(shown.warn.length, warns);
  const pkg = require("../package.json");
  const closeMenu = pkg.contributes.menus["view/item/context"].find((m) => m.command === "cswapStatus.apiClose");
  const re = new RegExp(closeMenu.when.match(/viewItem =~ \/(.+)\/$/)[1]);
  assert.deepStrictEqual(
    ["apiTask.cloud.cloud", "apiTask.cloud.running", "apiTask.cloud.done", "apiTask.cloud.closed", "apiTask.api.running"].map((v) => re.test(v)),
    [true, true, false, false, false]
  );

  // dispatch: the prompt (even one starting with a dash) goes on stdin, not argv
  worker.set({ start: { out: { id: "t44444444", key: "gmail", branch: null } }, list: { out: LIST } });
  answers.input.push("-x rewrite the parser", "3");
  answers.pick.push((items) => items.find((i) => i.key === "gmail"), (items) => [items[0]]);
  await T.dispatchTask();
  const start = worker.calls().find((c) => c.argv[0] === "start");
  assert.deepStrictEqual(start.argv, ["start", "--cwd", tmp, "--budget", "3", "--key", "gmail", "--allow-bash"]);
  assert.strictEqual(start.stdin, "-x rewrite the parser\n");
  assert.ok(shown.info.some((m) => m.includes("已派发 t44444444")));
  // a start that prints no JSON still confirms
  worker.set({ start: { out: "queued" }, list: { out: LIST } });
  answers.input.push("another", "1");
  answers.pick.push((items) => items[0], () => []);
  await T.dispatchTask();
  assert.strictEqual(shown.info.pop(), "已派发任务");

  // keys from the side bar: the key goes only to stdin; a budget change sends
  // no stdin (api-worker then keeps the stored key)
  worker.set({ "set-key": { out: "ok" }, "remove-key": { out: "ok" }, list: { out: LIST } });
  const keyTree = new T.ApiTree();
  const keysGroup = keyTree.getChildren()[0];
  assert.strictEqual(keysGroup.contextValue, "apiKeys");
  const keyRows = keyTree.getChildren(keysGroup);
  assert.deepStrictEqual(keyRows.map((k) => k.contextValue), ["apiKey", "apiKey"]);
  const secret = "sk-ant-api03-" + "k".repeat(30);
  answers.input.push("work", "50", `  ${secret}  `);
  await T.apiView.addKey();
  const setKeys = () => worker.calls().filter((c) => c.argv[0] === "set-key");
  assert.deepStrictEqual(setKeys().map((c) => [c.argv, c.stdin]), [[["set-key", "--name", "work", "--budget", "50"], secret + "\n"]]);
  assert.strictEqual(shown.info.pop(), "已保存 key「work」，每月预算 $50.00。");
  answers.input.push("120");
  await T.apiView.setKeyBudget(keyRows[0]);
  assert.deepStrictEqual([setKeys()[1].argv, setKeys()[1].stdin], [["set-key", "--name", "jj", "--budget", "120"], ""]);
  answers.warn.push(undefined);
  await T.apiView.removeKey(keyRows[1]);
  answers.warn.push("删除");
  await T.apiView.removeKey(keyRows[1]);
  assert.deepStrictEqual(worker.calls().filter((c) => c.argv[0] === "remove-key").map((c) => c.argv), [["remove-key", "gmail"]]);
  // a name that is not a key name never reaches api-worker
  const callsBefore = worker.calls().length;
  await T.apiView.removeKey({ apiKey: { name: "--all" } });
  await T.apiView.setKeyBudget({ apiKey: { name: "a b" } });
  assert.strictEqual(worker.calls().length, callsBefore);

  // worker errors stay visible after a good list: tree warning row + status bar
  worker.set({ list: { err: JSON.stringify({ error: "boom from worker" }), code: 1 } });
  await T.refreshApi();
  assert.match(apiItem.text, /\$\(warning\)/);
  assert.match(apiItem.tooltip.value, /boom from worker/);
  let top = new T.ApiTree().getChildren();
  assert.strictEqual(top[0].label, "刷新失败：boom from worker");
  assert.strictEqual(top.length, 4); // the old data is still listed under it
  // unparseable output is an error too (status item and tree)
  worker.set({ list: { out: "not json" } });
  await T.refreshApi();
  top = new T.ApiTree().getChildren();
  assert.strictEqual(top[0].label, "刷新失败：api-worker list 的输出无法解析");
  assert.match(apiItem.tooltip.value, /无法解析/);
  worker.set({ list: { out: LIST } });
  await T.refreshApi();
  assert.notStrictEqual(new T.ApiTree().getChildren()[0].label.slice(0, 4), "刷新失败");

  // an older worker without --no-accounts: one retry without it, then never again
  worker.set({ list: { out: LIST, rejectFlag: "--no-accounts" } });
  const before = listCalls().length;
  await T.refreshApi();
  await T.refreshApi();
  assert.deepStrictEqual(listCalls().slice(before), [
    ["list", "--limit", "30", "--no-accounts"],
    ["list", "--limit", "30"],
    ["list", "--limit", "30"],
  ]);
  assert.ok(!new T.ApiTree().getChildren()[0].label.startsWith("刷新失败"));

  // a changed apiWorkerPath takes effect once the cached binary is reset
  const worker2 = fakeProgram(tmpDir("api2"), "api-worker");
  worker2.set({ list: { out: { ...LIST, keys: [] } } });
  stub.config.apiWorkerPath = worker2.bin;
  T.proc.resetBins();
  T.api.reset();
  await T.refreshApi();
  assert.strictEqual(worker2.calls().length, 1);
  const emptyTree = new T.ApiTree();
  assert.strictEqual(emptyTree.getChildren()[0].label, "本月预算（0 个 key）");
  // no keys yet: the row under it adds one
  assert.strictEqual(emptyTree.getChildren(emptyTree.getChildren()[0])[0].command.command, "cswapStatus.apiKeyAdd");

  ext.deactivate();
  console.log("api view tests: OK");
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
