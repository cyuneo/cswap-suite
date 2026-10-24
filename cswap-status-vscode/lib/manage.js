// Adding and managing accounts from the side bar, all through the cswap CLI.
// The only step that stays in a terminal is Anthropic's own login page
// (`claude auth login`, unchanged); the extension waits for it to finish and
// then runs `cswap add`. Nothing here reads or keeps tokens: a pasted
// setup-token / API key goes straight to `cswap add-token -` on stdin.

const vscode = require("vscode");
const { runtime } = require("./config");
const { resolveBin, exec, lastLine } = require("./proc");
const f = require("./format");
const accounts = require("./accounts");
const satellite = require("./satellite");

// How often to check whether the login finished, and when to give up
// (tests shorten these).
const timing = { pollMs: 4000, giveUpMs: 15 * 60 * 1000 };

// `claude auth status --json`: {loggedIn, email, orgId, ...}, or null.
async function authStatus(claude) {
  const r = await exec(claude, ["auth", "status", "--json"], { timeout: 20000 });
  const start = r.out.indexOf("{");
  if (start < 0) return null;
  try {
    return JSON.parse(r.out.slice(start));
  } catch (e) {
    return null;
  }
}

const who = (s) => (s && s.loggedIn ? `${s.email || ""}|${s.orgId || ""}` : "");
const activeRow = () => accounts.state.rows.find((r) => r.active);

async function addCurrent() {
  const r = await accounts.runCswap(["add"], { title: "把当前登录的账号加进 cswap…" });
  if (r) vscode.window.showInformationMessage(`已添加：${lastLine(r.out) || "完成"}`);
  return r;
}

async function addToken() {
  const token = await vscode.window.showInputBox({
    title: "用 setup-token 或 API key 添加账号",
    prompt: "粘贴 `claude setup-token` 生成的令牌，或 sk-ant-api 开头的 API key。只交给 cswap add-token，不会存到别处。",
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (!v || /^\s*sk-ant-[A-Za-z0-9]/.test(v) ? null : "应该以 sk-ant- 开头"),
  });
  if (!token || !token.trim()) return;
  const r = await accounts.runCswap(["add-token", "-"], { title: "添加令牌账号…", input: token.trim() + "\n" });
  if (r) vscode.window.showInformationMessage(`已添加：${lastLine(r.out) || "完成"}`);
}

// Opens Claude Code's official login in a terminal, then adds whichever
// account ends up logged in. Ends when the login changes, the terminal
// closes, or after 15 minutes.
async function loginAndAdd() {
  const claude = resolveBin(null, "claude");
  if (!claude) return vscode.window.showErrorMessage("找不到 claude 命令（~/.local/bin 或 PATH）");
  const before = await authStatus(claude);
  const current = activeRow();
  const notes = ["登录会换掉这台电脑当前的 Claude 登录，所有正在运行的 Claude 会话都会跟着换到新账号。"];
  const buttons = ["打开登录"];
  if (current) notes.push(`当前账号 #${current.number} 已经在 cswap 里，之后随时可以切回来。`);
  else if (before && before.loggedIn) {
    notes.push("当前登录的账号还没加进 cswap，换掉之前最好先保存它。");
    buttons.unshift("先保存当前账号，再登录");
  }
  const go = await vscode.window.showWarningMessage(notes.join("\n\n"), { modal: true }, ...buttons);
  if (!go) return;
  if (go === "先保存当前账号，再登录" && !(await addCurrent())) return;
  const email = await vscode.window.showInputBox({
    title: "登录新账号",
    prompt: "要登录的邮箱（可以留空；填了会预先填进登录页）",
    ignoreFocusOut: true,
  });
  if (email === undefined) return;
  const args = ["auth", "login"];
  if (email.trim()) args.push("--email", email.trim());
  const term = vscode.window.createTerminal({ name: "Claude 登录", shellPath: claude, shellArgs: args });
  term.show();
  vscode.window.showInformationMessage("按终端里的提示在浏览器登录。登录成功后会自动加进 cswap，终端可以关掉。");

  const was = who(before);
  let done = false;
  let timer;
  let closeSub;
  const finish = async (reason) => {
    if (done) return;
    done = true;
    clearInterval(timer);
    if (closeSub) closeSub.dispose();
    const now = await authStatus(claude);
    // After the terminal closed, add whatever is logged in (logging in to
    // the same account again only refreshes its slot in cswap).
    if (reason === "changed" || (reason === "closed" && who(now))) {
      await addCurrent();
      return;
    }
    vscode.window.showWarningMessage(reason === "timeout" ? "等了 15 分钟还没有登录成功，已停止等待。" : "没有检测到新的登录，没有添加账号。");
  };
  const started = Date.now();
  timer = setInterval(async () => {
    if (done) return;
    if (Date.now() - started > timing.giveUpMs) return finish("timeout");
    const s = await authStatus(claude);
    if (!done && who(s) && who(s) !== was) finish("changed");
  }, timing.pollMs);
  if (vscode.window.onDidCloseTerminal) {
    closeSub = vscode.window.onDidCloseTerminal((t) => {
      if (t === term) finish("closed");
    });
  }
}

async function addAccount() {
  const current = activeRow();
  const picks = [
    {
      label: "$(sign-in) 登录一个新账号并添加",
      detail: "打开 Claude 官方登录（浏览器），登录好后自动加进 cswap",
      action: "login",
    },
    {
      label: "$(add) 添加当前登录的账号",
      detail: current ? `已经在 cswap 里（#${current.number}），再加一次会更新它` : "当前登录还没加进 cswap",
      action: "current",
    },
    { label: "$(key) 粘贴 setup-token 或 API key", detail: "不用在这台电脑登录", action: "token" },
    { label: "$(cloud-download) 从服务器添加（setup-token）", detail: "这台电脑用服务器上的账号", action: "satellite" },
  ];
  const c = await vscode.window.showQuickPick(picks, { title: "添加账号", placeHolder: "怎么添加？" });
  if (!c) return;
  if (c.action === "login") return loginAndAdd();
  if (c.action === "current") return addCurrent();
  if (c.action === "token") return addToken();
  return satellite.addAccounts();
}

// ---- right-click actions on an account row

const numOf = (node) => {
  const n = node && node.accountNumber;
  return n && /^\d+$/.test(String(n)) ? String(n) : null;
};
const rowOf = (node) => (node && node.row) || accounts.state.rows.find((r) => String(r.number) === numOf(node)) || {};

async function setEnabled(node, enabled) {
  const n = numOf(node);
  if (!n) return;
  const r = await accounts.runCswap([enabled ? "enable" : "disable", n], { title: `${enabled ? "启用" : "停用"} #${n}…` });
  if (r) vscode.window.showInformationMessage(enabled ? `#${n} 已启用，自动切换会用它。` : `#${n} 已停用，自动切换不会再切到它。`);
}

async function setAlias(node) {
  const n = numOf(node);
  if (!n) return;
  const row = rowOf(node);
  const v = await vscode.window.showInputBox({
    title: `#${n} 的别名`,
    prompt: "一个短名字，显示在列表里；留空就去掉别名",
    value: row.name && !String(row.name).includes("@") ? row.name : "",
  });
  if (v === undefined) return;
  const args = v.trim() ? ["alias", n, v.trim()] : ["alias", n, "--unset"];
  await accounts.runCswap(args, { title: `设置 #${n} 的别名…` });
}

// Empty = follow the global threshold ("default" to cswap).
function limitInput(n, label, row, key) {
  const own = row[`${key}Source`] === "own";
  const threshold = accounts.state.data && accounts.state.data.autoswitch ? accounts.state.data.autoswitch.threshold : null;
  return vscode.window.showInputBox({
    title: `#${n} 的${label}个人上限`,
    prompt: `用到这个百分比就切走（1-100）；留空 = 跟随全局阈值${threshold != null ? ` ${f.pct(threshold)}%` : ""}`,
    value: own && row[key] != null ? String(row[key]) : "",
    validateInput: (v) => {
      if (!v.trim()) return null;
      const x = Number(v);
      return Number.isFinite(x) && x >= 1 && x <= 100 ? null : "填 1 到 100 的数字，或者留空";
    },
  });
}

async function setLimits(node) {
  const n = numOf(node);
  if (!n) return;
  const row = rowOf(node);
  const five = await limitInput(n, " 5 小时", row, "max5h");
  if (five === undefined) return;
  const seven = await limitInput(n, " 7 天", row, "max7d");
  if (seven === undefined) return;
  const r = await accounts.runCswap(
    ["policy", "set", n, "--max-5h", five.trim() || "default", "--max-7d", seven.trim() || "default"],
    { title: `设置 #${n} 的个人上限…` }
  );
  if (r) vscode.window.showInformationMessage(`#${n} 的上限：5h ${five.trim() || "跟随阈值"}，7d ${seven.trim() || "跟随阈值"}。自动切换下次检查就按这个来。`);
}

// 主账号 is cswap's reserve flag: used only when the other accounts are at
// their limits, left again as soon as one of them has room. There is one, so
// making an account the 主账号 clears the flag on any other.
async function setReserve(node, reserve) {
  const n = numOf(node);
  if (!n) return;
  const old = reserve ? accounts.state.rows.filter((x) => x.reserve && String(x.number) !== n && /^\d+$/.test(String(x.number))) : [];
  const r = await accounts.runCswap(["policy", "set", n, reserve ? "--reserve" : "--no-reserve"], {
    title: `${reserve ? "设为" : "取消"}主账号 #${n}…`,
  });
  if (!r) return;
  if (!reserve) return vscode.window.showInformationMessage(`#${n} 不再是主账号，和其他账号一样轮换。`);
  for (const x of old) await accounts.runCswap(["policy", "set", String(x.number), "--no-reserve"], { title: `取消主账号 #${x.number}…` });
  vscode.window.showInformationMessage(
    `#${n} 是主账号了：其他账号都到上限时才用它，其他账号一恢复就切回去。${old.length ? `（${old.map((x) => `#${x.number}`).join("、")} 不再是主账号）` : ""}`
  );
}

async function remove(node) {
  const n = numOf(node);
  if (!n) return;
  const row = rowOf(node);
  const ok = await vscode.window.showWarningMessage(
    `永久删除 #${n}（${row.name || ""}）？\n\ncswap 里保存的这个账号的登录会被删掉，以后要用得重新登录添加。${
      row.active ? "\n\n它是当前正在用的账号。" : ""
    }`,
    { modal: true },
    "删除"
  );
  if (ok !== "删除") return;
  const r = await accounts.runCswap(["remove", n], { title: `删除 #${n}…`, input: "y\n" });
  if (!r) return;
  // cswap remove keeps the account's limits/主账号 entry, which would show up
  // again as an unmanaged row; it goes too (keyed by email once the slot is gone)
  const email = row.email || (String(row.name || "").includes("@") ? row.name : "");
  if (row.configured && email && accounts.state.bin) {
    await exec(accounts.state.bin, ["policy", "remove", email], { timeout: 20000 });
    await runtime.refreshAccounts();
  }
  vscode.window.showInformationMessage(`#${n} 已从 cswap 删除。`);
}

module.exports = {
  addAccount,
  addCurrent,
  addToken,
  loginAndAdd,
  setEnabled,
  setAlias,
  setLimits,
  setReserve,
  remove,
  timing,
};
