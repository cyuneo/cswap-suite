// Adding and managing accounts from the 账号 view: the 添加账号 flows
// (official login then `cswap add`, current login, pasted token) and the
// right-click actions, against the stub `vscode` module and fake programs.
//   node test/manage.test.js
"use strict";

const assert = require("assert");
const path = require("path");
const stub = require("./vscode-stub");
const { tmpDir, fakeProgram, hermetic } = require("./fakes");

const tmp = tmpDir("manage");
const binDir = hermetic(tmp);
const cswap = fakeProgram(tmp, "cswap");
const claude = fakeProgram(binDir, "claude"); // found on PATH like the real one
Object.assign(stub.config, { path: cswap.bin, showApi: false, refreshSeconds: 60, keepWorkflowsEnabled: false });
const { shown, answers, terminals } = stub;

const ext = require("../extension.js");
const T = ext._test;
const M = T.manage;
M.timing.pollMs = 30;

const ROWS = [
  { number: "1", name: "a@x", active: true, enabled: true, usage5h: 2, usage7d: 26, max5h: 30, max7d: 98, max5hSource: "own", max7dSource: "threshold", status: "Active", state: "active", switchable: true },
  { number: "2", name: "work", active: false, enabled: false, usage5h: 9, usage7d: 3, max5h: 98, max7d: 98, status: "Disabled", state: "disabled", switchable: false, reserve: true },
];
const OK = {
  add: { out: "Added Account 3: c@x [personal]" },
  "add-token": { out: "Added Account 4: d@x [setup-token]" },
  enable: { out: "ok" },
  disable: { out: "ok" },
  alias: { out: "ok" },
  "policy set": { out: "ok" },
  "policy remove": { out: "Removed policy" },
  remove: { out: "Removed" },
};
const setCswap = (rows) => cswap.set({ ...OK, "policy list": { out: { schemaVersion: 1, accounts: rows, autoswitch: { threshold: 98 } } } });
const calls = (cmd) => cswap.calls().filter((c) => c.argv[0] === cmd);
const loggedIn = (email) => claude.set({ "auth status": { out: { loggedIn: true, email, orgId: "o1" } }, "auth login": { out: "" } });
// What VS Code passes a right-click command: the card's data-vscode-context.
const node = (n) => ({ webview: "cswapStatus.accounts", ...T.accountsWeb.cardContext(T.accounts.state.rows.find((r) => r.number === n)) });
const waitFor = async (cond) => {
  for (let i = 0; i < 100 && !cond(); i++) await stub.sleep(20);
  assert.ok(cond(), "timed out waiting");
};

(async () => {
  setCswap(ROWS);
  ext.activate({ extensionPath: path.join(tmp, "ext"), subscriptions: [] });
  await waitFor(() => T.accounts.state.loaded);

  // 添加当前登录的账号: plain `cswap add`, cswap's line shown
  answers.pick.push((items) => items.find((i) => i.action === "current"));
  await M.addAccount();
  assert.deepStrictEqual(calls("add").map((c) => c.argv), [["add"]]);
  assert.strictEqual(shown.info.pop(), "已添加：Added Account 3: c@x [personal]");

  // pasted token goes to `cswap add-token -` on stdin only
  answers.input.push("  sk-ant-oat01-abcdefghijklmnop  ");
  await M.addToken();
  const tok = calls("add-token");
  assert.deepStrictEqual(tok.map((c) => c.argv), [["add-token", "-"]]);
  assert.strictEqual(tok[0].stdin, "sk-ant-oat01-abcdefghijklmnop\n");
  assert.ok(!cswap.calls().some((c) => c.argv.join(" ").includes("sk-ant-")));

  // login: official `claude auth login` in a terminal (argv, no shell); when
  // the logged-in account changes, `cswap add` runs once
  loggedIn("a@x");
  answers.warn.push("打开登录");
  answers.input.push("new@x");
  const adds = calls("add").length;
  await M.loginAndAdd();
  const term = terminals.pop();
  assert.strictEqual(term.options.shellPath, claude.bin);
  assert.deepStrictEqual(term.options.shellArgs, ["auth", "login", "--email", "new@x"]);
  assert.match(shown.warn.pop(), /当前账号 #1 已经在 cswap 里/);
  await stub.sleep(150);
  assert.strictEqual(calls("add").length, adds); // same login: keep waiting
  loggedIn("new@x");
  await waitFor(() => calls("add").length === adds + 1);
  stub.closeTerminal(term); // closing afterwards adds nothing more
  await stub.sleep(150);
  assert.strictEqual(calls("add").length, adds + 1);

  // login closed without a login: nothing added, says so
  claude.set({ "auth status": { out: { loggedIn: false } }, "auth login": { out: "" } });
  answers.warn.push("打开登录");
  answers.input.push("");
  await M.loginAndAdd();
  const t2 = terminals.pop();
  assert.deepStrictEqual(t2.options.shellArgs, ["auth", "login"]);
  stub.closeTerminal(t2);
  await waitFor(() => shown.warn.includes("没有检测到新的登录，没有添加账号。"));
  assert.strictEqual(calls("add").length, adds + 1);

  // current login not in cswap: offer to save it first, then log in
  setCswap(ROWS.map((r) => ({ ...r, active: false })));
  await T.refresh();
  loggedIn("old@x");
  answers.warn.push("先保存当前账号，再登录");
  answers.input.push(undefined); // then cancel at the email box
  await M.loginAndAdd();
  assert.strictEqual(calls("add").length, adds + 2);
  assert.strictEqual(terminals.length, 0);
  setCswap(ROWS);
  await T.refresh();

  // right-click actions
  await M.setEnabled(node("2"), true);
  await M.setEnabled(node("1"), false);
  assert.deepStrictEqual(calls("enable").map((c) => c.argv), [["enable", "2"]]);
  assert.deepStrictEqual(calls("disable").map((c) => c.argv), [["disable", "1"]]);

  answers.input.push("");
  await M.setAlias(node("2"));
  answers.input.push("main");
  await M.setAlias(node("1"));
  assert.deepStrictEqual(calls("alias").map((c) => c.argv), [["alias", "2", "--unset"], ["alias", "1", "main"]]);

  answers.input.push("25", "");
  await M.setLimits(node("1"));
  await M.setReserve(node("1"), true);
  await M.setReserve(node("2"), false);
  assert.deepStrictEqual(calls("policy").filter((c) => c.argv[1] === "set").map((c) => c.argv), [
    ["policy", "set", "1", "--max-5h", "25", "--max-7d", "default"],
    ["policy", "set", "1", "--reserve"],
    ["policy", "set", "2", "--no-reserve"], // one 主账号: #2 was it
    ["policy", "set", "2", "--no-reserve"],
  ]);
  assert.strictEqual(shown.info.pop(), "#2 不再是主账号，和其他账号一样轮换。");
  assert.strictEqual(shown.info.pop(), "#1 是主账号了：其他账号都到上限时才用它，其他账号一恢复就切回去。（#2 不再是主账号）");

  answers.warn.push(undefined);
  await M.remove(node("2"));
  assert.strictEqual(calls("remove").length, 0);
  answers.warn.push("删除");
  await M.remove(node("2"));
  assert.deepStrictEqual(calls("remove").map((c) => [c.argv, c.stdin]), [[["remove", "2"], "y\n"]]);
  // its limits entry goes too, or it would come back as an unmanaged row
  answers.warn.push("删除");
  await M.remove({ accountNumber: "1", row: { number: "1", email: "a@x", configured: true } });
  assert.deepStrictEqual(calls("policy").filter((c) => c.argv[1] === "remove").map((c) => c.argv), [["policy", "remove", "a@x"]]);

  // nothing reaches cswap with a malformed number
  const before = cswap.calls().length;
  await M.setEnabled({ accountNumber: "2 --all" }, false);
  await M.remove({ accountNumber: "$(id)" });
  assert.strictEqual(cswap.calls().length, before);

  ext.deactivate();
  console.log("manage tests: OK");
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
