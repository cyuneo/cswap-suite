// The 自动切换 view: what the cswap auto service is doing and why. Fed by the
// `autoswitch` block of the same `cswap policy list --json` the 账号 view
// runs (no extra cswap process). For an older cswap without that block, only
// `systemctl --user is-active cswap-auto.service` is asked.
// Pause/resume run `cswap auto --pause <duration>` / `--resume`.

const vscode = require("vscode");
const { runtime, cfg } = require("./config");
const { findBin, resolveBin, exec, lastLine } = require("./proc");
const f = require("./format");
const accounts = require("./accounts");

const state = {
  loaded: false,
  error: "", // the accounts read failed
  block: null, // policy list `autoswitch`, or null for an older cswap
  rows: [],
  legacy: null, // systemctl is-active output when there is no block
  seenSwitchAt: undefined, // lastSwitch.at at the previous refresh (undefined = first)
  status: { kind: "none", paused: false },
};
const changed = new vscode.EventEmitter();

const nowSec = () => Date.now() / 1000;

function isPaused(pausedUntil, now) {
  if (pausedUntil === "forever") return true;
  return typeof pausedUntil === "number" && pausedUntil > now;
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

// kind: running | paused | stale | stopped | none (no status file yet) |
// missing (old cswap, no unit). `paused` is also set when stale/stopped.
function serviceStatus(block, legacy, now) {
  if (!block) {
    if (legacy === "active" || legacy === "activating" || legacy === "reloading") return { kind: "running", paused: false };
    if (legacy === "inactive" || legacy === "failed" || legacy === "deactivating") return { kind: "stopped", paused: false };
    return { kind: "missing", paused: false };
  }
  const svc = block.service;
  const paused = isPaused(block.pausedUntil, now) || !!(svc && !svc.stale && svc.paused);
  if (svc && svc.stale) return { kind: pidAlive(svc.pid) ? "stale" : "stopped", paused };
  if (!svc) return { kind: "none", paused };
  return { kind: paused ? "paused" : "running", paused };
}

async function legacyState() {
  const sc = findBin("systemctl", []);
  if (!sc) return "missing";
  const r = await exec(sc, ["--user", "is-active", "cswap-auto.service"], { timeout: 5000 });
  // exit 4: no such unit (it still prints "inactive")
  if (r.code === 4) return "missing";
  return (r.out.trim().split("\n")[0] || "missing").trim();
}

function notifyText(ls) {
  const move = `#${ls.from} → #${ls.to}`;
  if (ls.auto === false || ls.trigger === "manual") return `cswap 已切换 ${move}（手动）`;
  return `cswap 自动切换 ${move}（${f.triggerZh(ls.trigger) || "自动"}）`;
}

// Called after every accounts refresh with accounts.state.
async function update(acc) {
  const data = acc.data;
  state.loaded = true;
  state.error = acc.error || "";
  state.rows = acc.rows || [];
  state.block = data && data.autoswitch && typeof data.autoswitch === "object" ? data.autoswitch : null;
  state.legacy = !state.error && !state.block ? await legacyState() : null;

  if (state.block) {
    const ls = state.block.lastSwitch;
    const at = ls && ls.at ? ls.at : null;
    // a manual switch made from this window already said so in the status bar
    const own = ls && (ls.auto === false || ls.trigger === "manual") && Date.now() - runtime.ownSwitchAt < 60000;
    if (state.seenSwitchAt !== undefined && at && at !== state.seenSwitchAt && cfg("notifyOnSwitch", true) && !own) {
      vscode.window.showInformationMessage(notifyText(ls));
    }
    state.seenSwitchAt = at;
  }
  state.status = state.error ? { kind: "none", paused: false } : serviceStatus(state.block, state.legacy, nowSec());
  vscode.commands.executeCommand("setContext", "cswapAuto.state", state.status.kind);
  vscode.commands.executeCommand("setContext", "cswapAuto.paused", state.status.paused);
  vscode.commands.executeCommand("setContext", "cswapAuto.canPause", !!state.block && !state.status.paused);
  changed.fire();
  return state;
}

// Marker for the compact status bar entry: {text, warn}.
function statusMarker() {
  if (!state.loaded || state.error) return { text: "", warn: false };
  const k = state.status.kind;
  if (k === "stale" || k === "stopped") return { text: " $(error)", warn: false, tip: "自动切换没有在运行" };
  if (state.status.paused) return { text: " $(debug-pause)", warn: true, tip: "自动切换已暂停" };
  return { text: "", warn: false };
}

// ---- tree

const noEmail = (s) => String(s || "").replace(/\S*@\S*/g, "…");
const accountLabel = (r) => `#${r.number}${r.name && !String(r.name).includes("@") ? ` ${r.name}` : ""}`;

function item(label, icon, color, opts) {
  const o = opts || {};
  const it = new vscode.TreeItem(label, o.collapsible);
  it.iconPath = new vscode.ThemeIcon(icon, color ? new vscode.ThemeColor(color) : undefined);
  if (o.description !== undefined) it.description = o.description;
  if (o.tooltip !== undefined) it.tooltip = o.tooltip;
  if (o.contextValue) it.contextValue = o.contextValue;
  if (o.node) it.node = o.node;
  return it;
}

function md(lines) {
  const m = new vscode.MarkdownString();
  m.appendMarkdown(lines.filter((l) => l !== null && l !== undefined && l !== "").join("  \n"));
  return m;
}

function settingsChangedSinceStart(block) {
  const s = block.service && block.service.settings;
  if (!s) return false;
  return ["threshold", "weeklyThreshold", "intervalSeconds", "cooldownSeconds", "hysteresisPct", "strategy"].some(
    (k) => k in s && k in block && s[k] !== block[k]
  );
}

function pausedText(block, now) {
  const p = block && block.pausedUntil;
  if (p === "forever") return "一直暂停";
  if (typeof p === "number" && p > now) return `到 ${p - now > 86400 ? f.dayClock(p) : f.clock(p)}（还剩 ${f.span(p - now)}）`;
  return "已暂停";
}

function serviceRow(now) {
  const { block } = state;
  const st = state.status;
  if (!block) {
    const legacyCtx = { contextValue: "cswapAuto.legacy" };
    if (st.kind === "running")
      return item("自动切换 运行中", "pulse", "charts.green", { description: "systemctl", tooltip: "cswap-auto.service 正在运行（旧版 cswap 只能看到这一项）。", ...legacyCtx });
    if (st.kind === "stopped")
      return item("自动切换 已停止", "error", "charts.red", { description: state.legacy, tooltip: "cswap-auto.service 没有在运行。用 systemctl --user status cswap-auto 查看。", ...legacyCtx });
    return item("自动切换 本机未安装", "circle-slash", undefined, { tooltip: "这台机器上没有 cswap-auto.service。", ...legacyCtx });
  }
  const svc = block.service;
  const ctx = st.paused ? "cswapAuto.paused" : "cswapAuto.pausable";
  const restart = settingsChangedSinceStart(block);
  const tip = [];
  if (svc) {
    tip.push(`**cswap auto**（PID ${svc.pid || "?"}）`);
    if (svc.startedAt) tip.push(`启动于 ${f.dayClock(svc.startedAt)}`);
    if (svc.lastTickAt) tip.push(`上次检查 ${f.clock(svc.lastTickAt)}（${f.ago(svc.lastTickAt, now)}）`);
    if (svc.nextTickAt && st.kind === "running") tip.push(`下次检查约 ${f.clock(svc.nextTickAt)}`);
  }
  if (st.paused) tip.push("暂停期间仍会读取当前账号的用量，但不会切换账号；当前账号用到上限也不会切。");
  if (restart) tip.push("$(warning) 设置在服务启动后改过，需要重启 cswap-auto 才生效。");
  const tooltip = md(tip);
  tooltip.supportThemeIcons = true;
  const suffix = restart ? " · 设置已改，需重启" : "";
  if (st.kind === "stale")
    return item("自动切换 卡住了", "error", "charts.red", {
      description: `上次检查 ${f.ago(svc.lastTickAt, now)}${suffix}`,
      tooltip: md([`进程（PID ${svc.pid}）还在，但已经超过 3 个周期没有完成检查。`, tooltip.value]),
      contextValue: ctx,
    });
  if (st.kind === "stopped")
    return item("自动切换 已停止", "error", "charts.red", {
      description: `上次检查 ${f.ago(svc.lastTickAt, now)}${suffix}`,
      tooltip: md([`状态文件里的进程（PID ${svc.pid || "?"}）已经不在了。用 systemctl --user status cswap-auto 查看。`, tooltip.value]),
      contextValue: ctx,
    });
  if (st.kind === "none")
    return item(st.paused ? "自动切换 已暂停（没有运行）" : "自动切换 没有运行", "circle-slash", undefined, {
      description: "没有状态文件",
      tooltip: "这台机器上还没有 cswap auto 运行过（cswap-auto.service 没装或没启动）。",
      contextValue: ctx,
    });
  if (st.kind === "paused")
    return item("自动切换 已暂停", "debug-pause", "charts.yellow", { description: pausedText(block, now) + suffix, tooltip, contextValue: ctx });
  const every = block.intervalSeconds || (svc.settings && svc.settings.intervalSeconds);
  return item("自动切换 运行中", "pulse", "charts.green", {
    description: `${svc.startedAt ? `自 ${f.clock(svc.startedAt)}` : ""}${every ? ` · 每 ${every}s` : ""}${suffix}`,
    tooltip,
    contextValue: ctx,
  });
}

const hasOwnLimit = (r) => r.max5hSource === "own" || r.max7dSource === "own";

// The switch line, clickable to change it. Accounts with their own limits
// ignore it, so the row says how many there are.
function rulesRow(block) {
  const t = block.threshold;
  const weekly = block.weeklyThreshold !== null && block.weeklyThreshold !== undefined;
  const own = state.rows.filter(hasOwnLimit);
  const others = [];
  if (block.cooldownSeconds !== undefined) others.push(`冷却 ${Math.round(block.cooldownSeconds / 60)}m`);
  if (block.hysteresisPct !== undefined) others.push(`回差 ${f.pct(block.hysteresisPct)}`);
  if (block.strategy) others.push(block.strategy);
  const it = item(`切换阈值 ${f.pct(t)}%${weekly ? ` · 7d ${f.pct(block.weeklyThreshold)}%` : ""}`, "settings-gear", undefined, {
    description: own.length ? `${own.length === state.rows.length ? "全部" : `${own.length} 个`}账号用自己的上限` : others.join(" · "),
    contextValue: "cswapAuto.threshold",
    tooltip: md([
      "**自动切换规则**（点击修改阈值）",
      `阈值：${f.pct(t)}%（没有个人上限的账号用到这里就切）`,
      weekly ? `每周阈值：${f.pct(block.weeklyThreshold)}%` : "每周阈值：未设（7d 也用上面的阈值）",
      own.length
        ? `用自己上限的账号（不看阈值）：${own.map((r) => `#${r.number} 5h ${f.pct(r.max5h)}% · 7d ${f.pct(r.max7d)}%`).join("；")}。改：账号右键「个人上限…」，留空就跟随阈值`
        : "",
      `检查间隔：${block.intervalSeconds ?? "?"} 秒`,
      `冷却：${block.cooldownSeconds ?? "?"} 秒（只限制提前切换、回到副账号、时段、先用完）`,
      `回差：${f.pct(block.hysteresisPct)} 点（目标账号要比当前账号多这么多余量才会主动切）`,
      `策略：${block.strategy === "consume-first" ? "consume-first（先用完快要重置的账号）" : `${block.strategy || "best"}（选余量最多的账号）`}`,
    ]),
  });
  it.command = { command: "cswapStatus.autoThreshold", title: "设置自动切换阈值…" };
  return it;
}

const REASON_ICON = {
  "below-threshold": ["check", "charts.green"],
  "reserve-in-use": ["shield", "charts.yellow"],
  "reserve-hold": ["shield", "charts.yellow"],
  "below-personal-limit": ["check", "charts.green"],
  cooldown: ["watch"],
  "stale-usage": ["history"],
  "active-idle": ["history"],
  "active-usage-unknown": ["question"],
  "no-comparison": ["question"],
  "policy-exhausted": ["error", "charts.red"],
  "all-exhausted": ["error", "charts.red"],
  "no-candidates": ["error", "charts.red"],
  "no-viable-target": ["error", "charts.red"],
  "no-qualifying-candidate": ["error", "charts.red"],
  paused: ["debug-pause", "charts.yellow"],
  error: ["warning", "charts.yellow"],
};

function decisionRow(svc, now) {
  const [icon, color] = svc.trigger ? ["zap", "charts.yellow"] : REASON_ICON[svc.reason] || ["info"];
  const blocked = color === "charts.red";
  const verdict = svc.trigger ? `切换（${f.triggerZh(svc.trigger)}）` : blocked ? "无法切换" : "不切换";
  // A translated reason makes cswap's English prose detail redundant; numbers stay.
  const known = svc.reason && f.reasonZh(svc.reason) !== svc.reason;
  const prose = /^[a-z ;,.()'-]*$/i.test(svc.detail || "");
  const desc = [f.reasonZh(svc.reason), known && prose ? "" : noEmail(svc.detail), svc.lastTickAt ? f.ago(svc.lastTickAt, now) : ""].filter(Boolean).join(" · ");
  return item(`当前 #${svc.active || "?"}：${verdict}`, icon, color, {
    description: desc,
    tooltip: md([
      `**上次检查的结论**：${verdict}`,
      svc.reason ? `原因：${f.reasonZh(svc.reason)}（${svc.reason}）` : "",
      svc.trigger ? f.TRIGGER_HELP[svc.trigger] || "" : "",
      svc.detail ? `详情：${noEmail(svc.detail)}` : "",
      svc.lastTickAt ? `检查时间：${f.clock(svc.lastTickAt)}` : "",
      svc.nextTickAt ? `下次检查约 ${f.clock(svc.nextTickAt)}` : "",
    ]),
  });
}

function distanceRow(r, now) {
  const wins = [
    ["5h", r.usage5h, r.max5h, r.max5hSource, r.resetsAt5h],
    ["7d", r.usage7d, r.max7d, r.max7dSource, r.resetsAt7d],
  ];
  const known = wins.filter(([, u, m]) => u !== null && u !== undefined && m !== null && m !== undefined);
  const gaps = known.map(([w, u, m]) => [w, Number(m) - Number(u)]);
  const minGap = gaps.length ? Math.min(...gaps.map((g) => g[1])) : null;
  const binding = gaps.length ? gaps.find((g) => g[1] === minGap)[0] : null;
  const color = minGap === null ? undefined : minGap <= 0 ? "charts.red" : minGap <= f.NEAR_MARGIN ? "charts.yellow" : "charts.green";
  const lines = [`**#${r.number} 距离触发**`];
  for (const [w, u, m, src, reset] of wins) {
    let line = `${w} \`${f.usageBar(u)}\` ${f.pct(u)}% / ${f.pct(m)}%`;
    if (src) line += `（${f.SOURCE_ZH[src] || src}）`;
    if (u !== null && u !== undefined && m !== null && m !== undefined) {
      const g = Number(m) - Number(u);
      line += g > 0 ? ` · 还差 ${Math.round(g)} 点` : " · 已到线";
    }
    if (typeof reset === "number" && reset > now) line += ` · ${f.span(reset - now)}后重置（${f.clock(reset)}）`;
    lines.push(line);
  }
  if (binding) lines.push(`先到线的是 ${binding}`);
  if (typeof r.usageAgeSeconds === "number") lines.push(`用量是 ${f.span(r.usageAgeSeconds)}前读的${r.lastError ? `（最近一次读取出错：${r.lastError}）` : ""}`);
  if (f.rowState(r) === "reserve") lines.push("当前是主账号：副账号能用时会自动切回（回到副账号），受冷却限制。");
  return item(`#${r.number} 距离触发`, "dashboard", color, {
    description: `5h ${f.pct(r.usage5h)}→${f.pct(r.max5h)} · 7d ${f.pct(r.usage7d)}→${f.pct(r.max7d)}`,
    tooltip: md(lines),
  });
}

const EXCLUDED_ZH = [
  [/^disabled/, "已停用"],
  [/^quarantined/, "已隔离"],
  [/^reserve/, "主账号"],
  [/^off-schedule/, "不在时段"],
  [/^policy[- ]limit/, "到个人上限"],
  [/^over threshold/, "到阈值"],
  [/^usage unknown/, "用量未知"],
  [/^api[- ]key( account)?/, "API key 账号"],
  [/^no (stored )?backup/, "没有备份"],
  [/^no headroom/, "没有余量"],
];
// The window detail inside the parentheses, as cswap's policy reason writes it.
const DETAIL_ZH = [
  [/personal (\S+) limit:\s*/g, "个人 $1 上限 "],
  [/threshold (\S+):\s*/g, "阈值 $1 "],
];

function excludedZh(s) {
  let t = noEmail(s);
  for (const [re, zh] of EXCLUDED_ZH) {
    if (re.test(t)) {
      t = t.replace(re, zh);
      break;
    }
  }
  for (const [re, zh] of DETAIL_ZH) t = t.replace(re, zh);
  return t;
}

function nextRow(block, active) {
  const nc = block.nextCandidate;
  const from = active ? `#${active.number}` : "当前账号";
  const common = "到上限的切换（个人上限、到达限额、故障转移）不受冷却和回差限制。";
  const collapsible = vscode.TreeItemCollapsibleState.Collapsed;
  if (!nc)
    return item("没有可切换的账号", "error", "charts.red", {
      collapsible,
      description: "到上限时会停在当前账号",
      tooltip: md([`如果 ${from} 现在到上限，没有别的账号可以切过去。展开看每个账号为什么不行。`]),
      node: { kind: "candidates" },
    });
  return item(`下一个 #${nc.number}${nc.reserve ? "（主账号）" : ""}`, "arrow-right", nc.reserve ? "charts.yellow" : undefined, {
    collapsible,
    description: `预计 · 余量 ${f.pct(nc.headroomPct)}%`,
    tooltip: md([
      `如果 ${from} 现在到上限，会切到 **#${nc.number}**。`,
      nc.why ? `原因：${noEmail(nc.why)}` : "",
      nc.reserve ? "它是主账号：其他账号都到上限时才用，副账号能用后会自动切回（回到副账号）。" : "",
      common,
    ]),
    node: { kind: "candidates" },
  });
}

function candidateRows(block, rows) {
  const nc = block.nextCandidate;
  const excluded = block.excluded || {};
  const others = rows.filter((r) => r.number && !r.active);
  others.sort((a, b) => (nc && String(a.number) === String(nc.number) ? -1 : nc && String(b.number) === String(nc.number) ? 1 : 0));
  if (!others.length) return [item("没有其他账号", "info")];
  return others.map((r) => {
    const n = String(r.number);
    const usage = `5h ${f.pct(r.usage5h)}% · 7d ${f.pct(r.usage7d)}%`;
    if (nc && String(nc.number) === n)
      return item(accountLabel(r), "arrow-right", nc.reserve ? "charts.yellow" : "charts.green", {
        description: `下一个 · 余量 ${f.pct(nc.headroomPct)}%`,
        tooltip: md([noEmail(nc.why), usage]),
      });
    if (excluded[n] !== undefined)
      return item(accountLabel(r), "circle-slash", undefined, { description: excludedZh(excluded[n]), tooltip: md([`不会切过去：${excludedZh(excluded[n])}`, usage]) });
    return item(accountLabel(r), "account", undefined, {
      description: `可用 · ${usage}`,
      tooltip: f.rowState(r) === "reserve" ? "主账号：其他账号都到上限时才用" : "可以切过去，排在下一个之后",
    });
  });
}

function historyItem(h, now) {
  const manual = h.auto === false || h.trigger === "manual";
  return item(`${f.dayClock(h.at)} #${h.from} → #${h.to}`, manual ? "person" : "zap", undefined, {
    description: manual ? "手动" : f.triggerZh(h.trigger),
    tooltip: md([manual ? f.TRIGGER_HELP.manual : f.TRIGGER_HELP[h.trigger] || h.trigger, f.ago(h.at, now)]),
  });
}

class AutoTree {
  constructor() {
    this.onDidChangeTreeData = changed.event;
  }
  getTreeItem(node) {
    return node;
  }
  getChildren(node) {
    const now = nowSec();
    const { block } = state;
    if (node) {
      if (!block || !node.node) return [];
      if (node.node.kind === "candidates") return candidateRows(block, state.rows);
      if (node.node.kind === "history") return (block.history || []).slice(0, 20).map((h) => historyItem(h, now));
      return [];
    }
    if (!state.loaded) return [item("读取中…", "loading~spin")];
    if (state.error) return [item(state.error, "warning")];
    const out = [serviceRow(now)];
    if (!block) {
      out.push(item("cswap 版本太旧，没有自动切换状态", "info", undefined, { tooltip: "升级 cswap 后这里会显示规则、当前结论、下一个账号和切换记录。" }));
      return out;
    }
    out.push(rulesRow(block));
    const svc = block.service;
    if (svc && (svc.reason || svc.trigger || svc.outcome)) out.push(decisionRow(svc, now));
    const active = state.rows.find((r) => r.active);
    if (active) out.push(distanceRow(active, now));
    out.push(nextRow(block, active));
    if (typeof block.cooldownUntil === "number" && block.cooldownUntil > now)
      out.push(
        item(`冷却中 还剩 ${f.mmss(block.cooldownUntil - now)}`, "watch", undefined, {
          tooltip: "冷却只影响提前切换、回到副账号、时段和先用完；到上限的切换不受影响。",
        })
      );
    const ls = block.lastSwitch;
    if (ls && ls.at) {
      const manual = ls.auto === false || ls.trigger === "manual";
      out.push(
        item(`上次 #${ls.from} → #${ls.to} · ${manual ? "手动" : f.triggerZh(ls.trigger)}`, manual ? "person" : "zap", undefined, {
          description: `${f.clock(ls.at)}（${f.ago(ls.at, now)}）`,
          tooltip: md([manual ? f.TRIGGER_HELP.manual : f.TRIGGER_HELP[ls.trigger] || ls.trigger, f.dayClock(ls.at)]),
        })
      );
    }
    const hist = block.history || [];
    if (hist.length)
      out.push(
        item(`最近切换（${Math.min(hist.length, 20)} 次）`, "history", undefined, {
          collapsible: vscode.TreeItemCollapsibleState.Collapsed,
          node: { kind: "history" },
        })
      );
    for (const r of state.rows) {
      if (!r.quarantined) continue;
      out.push(
        item(`#${r.number} 已隔离 ${noEmail(r.quarantined)}`, "warning", "charts.red", {
          tooltip: `自动切换不会再用这个账号。用该账号登录后运行 cswap add --slot ${r.number}`,
        })
      );
    }
    return out;
  }
}

// ---- pause / resume

function cswapBin() {
  return accounts.state.bin || resolveBin("path", "cswap");
}

function minutesLeftToday() {
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return Math.max(1, Math.ceil((midnight - now) / 60000));
}

async function runAuto(args, doneText) {
  const bin = cswapBin();
  if (!bin) return vscode.window.showErrorMessage(accounts.state.error || "cswap 未找到");
  const r = await exec(bin, ["auto", ...args], { timeout: 20000 });
  if (r.code !== 0) {
    vscode.window.showErrorMessage(`cswap auto ${args[0]} 失败：${lastLine(r.err) || lastLine(r.out) || `退出码 ${r.code}`}`);
  } else {
    vscode.window.setStatusBarMessage(`cswap：${doneText}`, 5000);
  }
  await runtime.refreshAccounts();
  return r;
}

async function pause() {
  if (!state.block) {
    vscode.window.showWarningMessage("这个 cswap 版本太旧，不支持暂停自动切换（需要 cswap auto --pause）。");
    return;
  }
  const pick = await vscode.window.showQuickPick(
    [
      { label: "30 分钟", dur: "30m" },
      { label: "2 小时", dur: "2h" },
      { label: "今天剩下的时间", dur: `${minutesLeftToday()}m` },
      { label: "一直暂停", description: "直到手动恢复", dur: "forever" },
    ],
    { title: "暂停自动切换多久？" }
  );
  if (!pick) return;
  const host = f.where().label;
  const ok = await vscode.window.showWarningMessage(
    `在 ${host} 上暂停 cswap 自动切换（${pick.label}）？暂停期间当前账号用到上限也不会自动切换。`,
    { modal: true },
    "暂停"
  );
  if (ok !== "暂停") return;
  return runAuto(["--pause", pick.dur], `自动切换已暂停（${pick.label}）`);
}

async function resume() {
  if (!state.block) {
    vscode.window.showWarningMessage("这个 cswap 版本太旧，不支持暂停/恢复自动切换（需要 cswap auto --resume）。");
    return;
  }
  const host = f.where().label;
  const ok = await vscode.window.showWarningMessage(`在 ${host} 上恢复 cswap 自动切换？`, { modal: true }, "恢复");
  if (ok !== "恢复") return;
  return runAuto(["--resume"], "自动切换已恢复");
}

// ---- threshold

const lineInput = (title, prompt, value, optional) =>
  vscode.window.showInputBox({
    title,
    prompt,
    value,
    validateInput: (v) => {
      if (optional && !v.trim()) return null;
      const x = Number(v);
      return v.trim() && Number.isFinite(x) && x >= 50 && x <= 99.9 ? null : `填 50 到 99.9 的数字${optional ? "，或者留空" : ""}`;
    },
  });

async function setThreshold() {
  const block = state.block;
  if (!block || typeof block.threshold !== "number") {
    vscode.window.showWarningMessage("这个 cswap 版本太旧，不能在这里设置阈值（终端里运行 cswap config set autoswitch.threshold 90）。");
    return;
  }
  const weekly = typeof block.weeklyThreshold === "number" ? String(block.weeklyThreshold) : "";
  const t = await lineInput("自动切换阈值", "账号的 5 小时用量到这个百分比就自动切换（50-99.9）", String(block.threshold), false);
  if (t === undefined) return;
  const w = await lineInput("每周阈值（7 天）", "7 天用量到这个百分比就切换；留空 = 跟上面的阈值一样", weekly, true);
  if (w === undefined) return;
  if (!(await accounts.runCswap(["config", "set", "autoswitch.threshold", t.trim()], { title: "设置自动切换阈值…", quiet: true }))) return;
  const wa = w.trim() ? ["config", "set", "autoswitch.weeklyThreshold", w.trim()] : ["config", "unset", "autoswitch.weeklyThreshold"];
  if (w.trim() !== weekly && !(await accounts.runCswap(wa, { title: "设置每周阈值…", quiet: true }))) return;
  const done = `自动切换阈值 ${t.trim()}%${w.trim() ? `，7 天 ${w.trim()}%` : ""}`;
  const own = state.rows.filter(hasOwnLimit).filter((r) => /^\d+$/.test(String(r.number)));
  if (!own.length) return vscode.window.setStatusBarMessage(`$(check) ${done}，自动切换下次检查就按这个来`, 8000);
  const list = own.map((r) => `#${r.number}（5h ${f.pct(r.max5h)}% · 7d ${f.pct(r.max7d)}%）`).join("、");
  const pick = await vscode.window.showInformationMessage(
    `${done}。但 ${list}设了自己的上限，按自己的上限切换，不看这个阈值。`,
    "都改为跟随阈值"
  );
  if (pick !== "都改为跟随阈值") return;
  for (const r of own)
    await accounts.runCswap(["policy", "set", String(r.number), "--max-5h", "default", "--max-7d", "default"], {
      title: `#${r.number} 改为跟随阈值…`,
      quiet: true,
    });
  vscode.window.setStatusBarMessage(`$(check) ${own.map((r) => `#${r.number}`).join("、")} 改为跟随阈值 ${t.trim()}%`, 8000);
}

module.exports = { state, changed, update, serviceStatus, isPaused, statusMarker, AutoTree, pause, resume, setThreshold, minutesLeftToday, excludedZh };
