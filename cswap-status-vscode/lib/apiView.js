// The API worker status bar entry, the API Worker view in the cswap side bar
// (keys, running tasks, recent tasks), the task actions and adding / budgeting
// / removing keys.

const vscode = require("vscode");
const { barMode, showItem } = require("./config");
const { money, usageBar, isRunning, isFinal, taskStateZh, defaultCwd } = require("./format");
const api = require("./api");

const API_ICON = {
  queued: "clock",
  running: "sync~spin",
  launching: "sync~spin",
  done: "pass",
  failed: "error",
  cancelled: "circle-slash",
  lost: "question",
  cloud: "cloud",
};
const iconOf = (t) => API_ICON[t.state] || "circle-outline";

let item = null;

function init(statusItem) {
  item = statusItem;
}

function render() {
  if (!item) return;
  const { data, error } = api.state;
  if (!api.enabled() || !api.available()) {
    item.hide();
    return;
  }
  if (!data) {
    if (!error) return; // first read still running
    item.text = "$(cloud) API $(warning)";
    item.tooltip = error;
    item.backgroundColor = undefined;
    showItem(item);
    return;
  }
  const keys = data.keys || [];
  const tasks = data.tasks || [];
  const running = tasks.filter(isRunning);
  const left = keys.reduce((s, k) => s + (Number(k.leftUsd) || 0), 0);
  const low = keys.length && keys.every((k) => Number(k.leftUsd) < 0.1 * Number(k.monthlyBudgetUsd || 1));
  const spin = running.length ? "sync~spin" : "cloud";
  item.text =
    barMode() === "compact"
      ? `$(${spin}) ${keys.length ? (left >= 10 ? `$${Math.floor(left)}` : money(left)) : "API"}${running.length ? ` ${running.length}` : ""}`
      : `$(${spin}) API ${keys.length ? "剩 " + money(left) : "未设 key"}${running.length ? ` · ${running.length} 个运行中` : ""}`;
  if (error) item.text += " $(warning)";
  item.backgroundColor = low ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  const md = new vscode.MarkdownString();
  md.supportThemeIcons = true;
  if (error) md.appendMarkdown(`$(warning) 上次刷新失败（下面是之前的数据）：${error}\n\n`);
  md.appendMarkdown("**API worker 本月预算**（api-worker 自己记的账，不是 Console 的实际余额）\n\n");
  if (keys.length) {
    md.appendMarkdown("| key | 已用 | 预留 | 上限 | 剩余 |\n|---|---|---|---|---|\n");
    for (const k of keys) {
      md.appendMarkdown(`| ${k.name} | ${money(k.spentUsd)} | ${money(k.reservedUsd)} | ${money(k.monthlyBudgetUsd)} | ${money(k.leftUsd)} |\n`);
    }
  } else {
    md.appendMarkdown("还没有设置 API key（在 cswap 侧栏的 API Worker 里添加）。\n");
  }
  if (tasks.length) {
    md.appendMarkdown("\n**最近的任务**\n\n");
    for (const t of tasks.slice(0, 8)) {
      md.appendMarkdown(`$(${iconOf(t)}) ${api.taskLine(t)} — ${(t.title || "").replace(/[|*_`]/g, " ")}  \n`);
    }
  }
  md.appendMarkdown("\n点击打开 cswap 侧栏");
  item.tooltip = md;
  showItem(item);
}

function announce(finished) {
  for (const t of finished) {
    const fn = t.state === "done" ? vscode.window.showInformationMessage : vscode.window.showWarningMessage;
    fn(`${t.kind === "cloud" ? "云端任务" : "API 任务"} ${t.id} ${taskStateZh(t.state)}：${t.title || ""}`, "查看结果").then((c) => {
      if (c) openTaskResult(t.id);
    });
  }
}

async function refresh() {
  const finished = await api.refresh();
  render();
  announce(finished);
}

function group(id, label, icon, expanded) {
  const it = new vscode.TreeItem(label, expanded ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed);
  it.iconPath = new vscode.ThemeIcon(icon);
  it.groupId = id;
  return it;
}

// The API Worker view in the cswap side bar: keys, running tasks, recent tasks.
class ApiTree {
  constructor() {
    this.onDidChangeTreeData = api.changed.event;
  }
  getTreeItem(node) {
    return node;
  }
  getChildren(node) {
    if (!api.enabled()) return [];
    const { data, error } = api.state;
    if (!data) {
      const it = new vscode.TreeItem(error || (api.available() ? "读取中…" : "api-worker 未安装"));
      it.iconPath = new vscode.ThemeIcon(error ? "warning" : "info");
      it.tooltip = error || undefined;
      return [it];
    }
    const keys = data.keys || [];
    const tasks = data.tasks || [];
    if (!node) {
      const running = tasks.filter(isRunning);
      const out = [];
      if (error) {
        // the data below is from the last good read; say so
        const e = new vscode.TreeItem(`刷新失败：${error}`);
        e.iconPath = new vscode.ThemeIcon("warning", new vscode.ThemeColor("charts.yellow"));
        e.description = "下面是之前的数据";
        e.tooltip = error;
        out.push(e);
      }
      const keysGroup = group("keys", `本月预算（${keys.length} 个 key）`, "credit-card", true);
      keysGroup.contextValue = "apiKeys"; // ＋ 添加 key
      out.push(
        keysGroup,
        group("running", `进行中（${running.length}）`, "sync", running.length > 0),
        group("recent", `最近的任务（${tasks.length - running.length}）`, "history", true)
      );
      return out;
    }
    if (node.groupId === "keys") {
      if (!keys.length) {
        const it = new vscode.TreeItem("还没有 key：点这里添加");
        it.iconPath = new vscode.ThemeIcon("add");
        it.command = { command: "cswapStatus.apiKeyAdd", title: "添加 API key" };
        return [it];
      }
      return keys.map((k) => {
        const cap = Number(k.monthlyBudgetUsd) || 0;
        const used = (Number(k.spentUsd) || 0) + (Number(k.reservedUsd) || 0);
        const ratio = cap ? Math.min(1, used / cap) : 0;
        const it = new vscode.TreeItem(k.name);
        it.description = `${usageBar(ratio * 100)} 剩 ${money(k.leftUsd)} / ${money(cap)}`;
        it.tooltip = `本月 api-worker 任务已用 ${money(k.spentUsd)}，进行中预留 ${money(k.reservedUsd)}，你设的每月预算 ${money(cap)}。\n只算 api-worker 派的任务，不含这把 key 在别处的花费，也不是 Console 里的实际余额。`;
        it.iconPath = new vscode.ThemeIcon("key", new vscode.ThemeColor(ratio >= 0.9 ? "charts.red" : ratio >= 0.7 ? "charts.yellow" : "charts.green"));
        it.contextValue = "apiKey";
        it.apiKey = k;
        return it;
      });
    }
    const running = node.groupId === "running";
    return tasks
      .filter((t) => isRunning(t) === running)
      .map((t) => {
        const it = new vscode.TreeItem(t.title || t.id);
        it.description = api.taskLine(t);
        it.tooltip = new vscode.MarkdownString(
          [
            `**${t.title || t.id}**`,
            `状态：${taskStateZh(t.state)}`,
            t.kind === "cloud" ? `账号：${t.account || "-"}` : `key：${t.key || "-"} · 预算 ${money(t.budgetUsd)}`,
            t.costUsd != null ? `花费：${money(t.costUsd)}` : "",
            t.branch || t.resultBranch ? `分支：${t.branch || t.resultBranch}` : "",
            t.lastActivity ? `最近：${String(t.lastActivity).slice(0, 200)}` : "",
            t.error ? `错误：${t.error}` : "",
          ]
            .filter(Boolean)
            .join("  \n")
        );
        it.iconPath = new vscode.ThemeIcon(iconOf(t));
        it.contextValue = `apiTask.${t.kind || "api"}.${t.state}`;
        it.command = { command: "cswapStatus.apiOpenResult", title: "查看结果", arguments: [t.id] };
        it.taskId = t.id;
        it.task = t;
        return it;
      });
  }
}

const taskIdOf = (arg) => (typeof arg === "string" ? arg : arg && arg.taskId);

async function openTaskResult(arg) {
  const id = taskIdOf(arg);
  if (!id) return;
  const r = await vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: `读取任务 ${id}…` }, () =>
    api.run(["result", id], { timeout: 180000 })
  );
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || `读取 ${id} 失败`);
  let t;
  try {
    t = JSON.parse(r.out);
  } catch (e) {
    return vscode.window.showErrorMessage("结果无法解析");
  }
  const lines = [`# ${t.title || id}`, ""];
  lines.push(`- 任务：\`${id}\`（${t.kind === "cloud" ? "云端会话" : "API worker"}）`);
  lines.push(`- 状态：**${taskStateZh(t.state)}**`);
  if (t.kind === "cloud") {
    if (t.account) lines.push(`- 账号：${t.account}`);
    if (t.sessionUrl) lines.push(`- 会话：${t.sessionUrl}`);
    if (t.resultBranch) lines.push(`- 结果分支：\`${t.resultBranch}\``);
    if (t.howToMerge) lines.push(`- 合并：\`${t.howToMerge}\``);
    if (t.howToGet) lines.push(`- 取回文件：\`${t.howToGet}\``);
  } else {
    lines.push(`- key：${t.key || "-"} · 模型：${t.model || "-"} · 预算 ${money(t.budgetUsd)} · 花费 ${money(t.costUsd)}`);
    if (t.workdir) lines.push(`- 目录：\`${t.workdir}\``);
    if (t.branch) lines.push(`- 分支：\`${t.branch}\``);
  }
  if (t.error) lines.push(`- 错误：${t.error}`);
  if (t.note) lines.push(`- 说明：${t.note}`);
  if (t.outputs && t.outputs.length) lines.push("", "## 输出文件", "", ...t.outputs.map((o) => `- ${o}`));
  if (t.commits && t.commits.length) lines.push("", "## 提交", "", "```", ...t.commits, "```");
  if (t.diffStat) lines.push("", "## 改动", "", "```", t.diffStat, "```");
  lines.push("", "## 报告", "", t.result || (isFinal(t) ? "（没有报告）" : "（任务还没做完）"));
  const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: lines.join("\n") });
  await vscode.window.showTextDocument(doc, { preview: true });
}

async function cancelTask(arg) {
  const id = taskIdOf(arg);
  if (!id) return;
  const ok = await vscode.window.showWarningMessage(`停止任务 ${id}？`, { modal: true }, "停止");
  if (ok !== "停止") return;
  const r = await api.run(["cancel", id]);
  if (r.code !== 0) vscode.window.showErrorMessage(r.err || "停止失败");
  refresh();
}

async function openSession(arg) {
  const t = arg && arg.task;
  const url = t && t.sessionUrl;
  if (url && /^https:\/\/claude\.ai\//.test(url)) vscode.env.openExternal(vscode.Uri.parse(url));
}

async function collectTask(arg) {
  const id = taskIdOf(arg);
  if (!id) return;
  const r = await api.run(["collect", id], { timeout: 180000 });
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || "取回失败");
  let out = null;
  try {
    out = JSON.parse(r.out);
  } catch (e) {
    /* below */
  }
  if (!out || !out.to) return vscode.window.showInformationMessage(`已取回任务 ${id} 的输出文件`);
  const go = await vscode.window.showInformationMessage(`已取回 ${(out.files || []).length} 个文件到 ${out.to}`, "打开文件夹");
  if (go) vscode.commands.executeCommand("revealFileInOS", vscode.Uri.file(out.to));
}

async function dispatchTask() {
  if (!api.available()) return vscode.window.showErrorMessage("没有找到 api-worker（先运行它的 install.sh）。");
  const prompt = await vscode.window.showInputBox({
    title: "派给 API worker（花 Console API 额度）",
    prompt: "任务内容。后台任务没法问你问题，要写清楚改哪些文件、做到什么程度算完成。",
    ignoreFocusOut: true,
  });
  if (!prompt || !prompt.trim()) return;
  const keys = (api.state.data && api.state.data.keys) || [];
  let key = null;
  if (keys.length > 1) {
    const pick = await vscode.window.showQuickPick(
      [{ label: "自动（本月剩得最多的）", key: null }, ...keys.map((k) => ({ label: k.name, description: `剩 ${money(k.leftUsd)}`, key: k.name }))],
      { title: "用哪个 key" }
    );
    if (!pick) return;
    key = pick.key;
  }
  const budget = await vscode.window.showInputBox({
    title: "这个任务最多花多少美元",
    value: "2",
    validateInput: (v) => (Number(v) > 0 ? null : "填一个大于 0 的数"),
  });
  if (!budget) return;
  const perms = await vscode.window.showQuickPick(
    [
      { label: "允许运行命令（跑测试、构建）", flag: "--allow-bash" },
      { label: "允许上网", flag: "--allow-web" },
    ],
    { title: "额外权限（可以都不选）", canPickMany: true }
  );
  if (perms === undefined) return;
  // The prompt goes on stdin (`api-worker start` reads it when --prompt is
  // absent): not visible in `ps`, no argv length limit.
  const args = ["start", "--cwd", defaultCwd(), "--budget", String(Number(budget))];
  if (key) args.push("--key", key);
  for (const p of perms) args.push(p.flag);
  const r = await api.run(args, { timeout: 120000, input: prompt.trim() + "\n" });
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || "派发失败");
  let t = null;
  try {
    t = JSON.parse(r.out);
  } catch (e) {
    /* below */
  }
  vscode.window.showInformationMessage(
    t && t.id ? `已派发 ${t.id}${t.branch ? `，分支 ${t.branch}` : ""}${t.key ? `（key ${t.key}）` : ""}` : "已派发任务"
  );
  refresh();
}

// ---- keys. The key itself goes to `api-worker set-key` on stdin only: never
// into argv, the extension's state or a log.

const KEY_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/;
const usd = (v) => (Number(v) > 0 ? null : "填一个大于 0 的数（美元）");
const BUDGET_HINT = "按 Console 里这把 key 每月实际能用的金额填（例如订阅附带的 API 额度），侧栏的「剩」才接近实际。";

async function addKey() {
  if (!api.available()) return vscode.window.showErrorMessage("没有找到 api-worker（先运行它的 install.sh）。");
  const keys = (api.state.data && api.state.data.keys) || [];
  const name = await vscode.window.showInputBox({
    title: "添加 API key（1/3）：名字",
    prompt: "起个名字，例如账号名；派任务时用它选 key。用已有的名字会换掉那把 key。",
    value: keys.length ? "" : "default",
    validateInput: (v) => (KEY_NAME.test(v.trim()) ? null : "字母、数字、点、下划线、横线，最多 32 个字符"),
  });
  if (!name || !name.trim()) return;
  const old = keys.find((k) => k.name === name.trim());
  const budget = await vscode.window.showInputBox({
    title: "添加 API key（2/3）：每月预算（美元）",
    prompt: BUDGET_HINT,
    value: old ? String(old.monthlyBudgetUsd) : "",
    validateInput: usd,
  });
  if (!budget) return;
  const key = await vscode.window.showInputBox({
    title: "添加 API key（3/3）：key",
    prompt: "粘贴 Console（platform.claude.com）里创建的 API key。只交给 api-worker 保存（权限 600），不会显示或记录。",
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (!v || /^\s*sk-ant-(api\d\d|usr[0-9a-z]*)-/.test(v) ? null : "应该以 sk-ant-api 或 sk-ant-usr 开头"),
  });
  if (!key || !key.trim()) return;
  const r = await api.run(["set-key", "--name", name.trim(), "--budget", String(Number(budget))], { input: key.trim() + "\n" });
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || "保存 key 失败");
  vscode.window.showInformationMessage(`已保存 key「${name.trim()}」，每月预算 ${money(budget)}。`);
  refresh();
}

const keyOf = (node) => {
  const k = node && node.apiKey;
  return k && KEY_NAME.test(String(k.name)) ? k : null;
};

async function setKeyBudget(node) {
  const k = keyOf(node);
  if (!k) return;
  const v = await vscode.window.showInputBox({
    title: `key「${k.name}」的每月预算（美元）`,
    prompt: BUDGET_HINT,
    value: String(k.monthlyBudgetUsd ?? ""),
    validateInput: usd,
  });
  if (!v) return;
  // no key on stdin: api-worker changes only the budget
  const r = await api.run(["set-key", "--name", k.name, "--budget", String(Number(v))]);
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || "设置预算失败");
  vscode.window.showInformationMessage(`key「${k.name}」的每月预算改为 ${money(v)}。`);
  refresh();
}

async function removeKey(node) {
  const k = keyOf(node);
  if (!k) return;
  const ok = await vscode.window.showWarningMessage(
    `删除 key「${k.name}」？\n\n只删掉 api-worker 保存的这一份。Console 里的 key 还能用，要作废请去 Console。`,
    { modal: true },
    "删除"
  );
  if (ok !== "删除") return;
  const r = await api.run(["remove-key", k.name]);
  if (r.code !== 0) return vscode.window.showErrorMessage(r.err || "删除 key 失败");
  vscode.window.showInformationMessage(`已删除 key「${k.name}」。`);
  refresh();
}

async function showApi() {
  await refresh();
  const tasks = (api.state.data && api.state.data.tasks) || [];
  const picks = tasks.map((t) => ({
    label: `$(${iconOf(t)}) ${t.title || t.id}`,
    description: api.taskLine(t),
    id: t.id,
  }));
  picks.unshift({ label: "$(add) 派一个新任务给 API worker", action: "dispatch" });
  const c = await vscode.window.showQuickPick(picks, { title: "API worker", placeHolder: "选任务查看结果" });
  if (!c) return;
  if (c.action === "dispatch") return dispatchTask();
  openTaskResult(c.id);
}

module.exports = {
  init,
  render,
  refresh,
  ApiTree,
  openTaskResult,
  cancelTask,
  openSession,
  collectTask,
  dispatchTask,
  showApi,
  addKey,
  setKeyBudget,
  removeKey,
};
