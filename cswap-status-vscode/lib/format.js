// Shared text helpers and the display-only maps from cswap / api-worker
// words to Chinese. Logic never switches on the Chinese text.

const vscode = require("vscode");
const os = require("os");

function where() {
  const remote = vscode.env.remoteName; // "ssh-remote", "wsl", ... or undefined
  const host = os.hostname().replace(/\.local$/, "");
  if (!remote) return { icon: "$(device-desktop)", label: `本机 ${host}`, remote: false };
  const kind = remote === "ssh-remote" ? "SSH" : remote.toUpperCase();
  return { icon: "$(remote)", label: `${kind} ${host}`, remote: true };
}

function pct(v) {
  return v === null || v === undefined ? "?" : `${Math.round(v)}`;
}

function usageText(r) {
  return `5h ${pct(r.usage5h)}/${pct(r.max5h)}% · 7d ${pct(r.usage7d)}/${pct(r.max7d)}%`;
}

// A 10-cell bar for a percentage (0-100).
function usageBar(v) {
  const n = Math.max(0, Math.min(10, Math.round((Number(v) || 0) / 10)));
  return "█".repeat(n) + "░".repeat(10 - n);
}

const money = (v) => `$${(Number(v) || 0).toFixed(2)}`;

function defaultCwd() {
  const folders = vscode.workspace.workspaceFolders || [];
  return folders.length ? folders[0].uri.fsPath : os.homedir();
}

// ---- accounts

// Machine state of a `policy list --json` row. Newer cswap sends `state`;
// older ones only the English status text, which is mapped here as a fallback.
function rowState(r) {
  if (r.state) return r.state;
  const s = String(r.status || "");
  if (r.enabled === false || s === "Disabled") return "disabled";
  if (s.startsWith("Policy-limit")) return "limit";
  if (s === "Off-schedule") return "off-schedule";
  if (s === "Usage unknown") return "unknown";
  if (s === "Not managed") return "unmanaged";
  if (s === "Reserve") return "reserve";
  return r.active ? "active" : "ready";
}

// Over its line or outside its schedule: the status bar turns yellow.
function isWarn(r) {
  const st = rowState(r);
  return st === "limit" || st === "off-schedule";
}

// Within 15 points of the row's own 5h/7d line (the engine starts looking
// at every candidate at that margin).
const NEAR_MARGIN = 15;
function nearLimit(r) {
  const near = (u, m) => u !== null && u !== undefined && m !== null && m !== undefined && Number(u) >= Number(m) - NEAR_MARGIN;
  return near(r.usage5h, r.max5h) || near(r.usage7d, r.max7d);
}

// The status bar's short form: the window closest to its line ("5h 34/40%";
// the line after the slash only when it is the account's own, the shared
// threshold being the same for every account). 5h when neither is known.
function compactUsage(r) {
  const wins = [
    ["5h", r.usage5h, r.max5h, r.max5hSource],
    ["7d", r.usage7d, r.max7d, r.max7dSource],
  ];
  const known = (u, m) => u !== null && u !== undefined && m !== null && m !== undefined;
  let pick = wins[0];
  for (const w of wins) {
    if (!known(w[1], w[2])) continue;
    if (!known(pick[1], pick[2]) || Number(w[2]) - Number(w[1]) < Number(pick[2]) - Number(pick[1])) pick = w;
  }
  const [label, used, max, source] = pick;
  return `${label} ${pct(used)}${source === "own" && max !== null && max !== undefined ? `/${pct(max)}` : ""}%`;
}

const STATE_ZH = {
  active: "当前",
  ready: "可用",
  disabled: "已停用",
  limit: "到上限",
  "off-schedule": "不在时段",
  reserve: "主账号",
  unknown: "用量未知",
  unmanaged: "未管理",
};

const STATUS_ZH = {
  Active: "当前",
  Ready: "可用",
  Reserve: "主账号",
  Disabled: "已停用",
  "Off-schedule": "不在时段",
  "Usage unknown": "用量未知",
  "Not managed": "未管理",
};

// The row's status in Chinese, e.g. "到上限（5h）", "主账号".
function statusZh(r) {
  const s = String(r.status || "");
  if (r.state) {
    if (r.state === "limit") {
      const w = r.blockedWindow || (s.match(/^Policy-limit \((.+)\)$/) || [])[1];
      return w ? `到上限（${w}）` : STATE_ZH.limit;
    }
    // an active reserve account is "reserve"; the side bar already says 当前
    return STATE_ZH[r.state] || r.state;
  }
  const m = s.match(/^Policy-limit \((.+)\)$/);
  if (m) return `到上限（${m[1]}）`;
  return STATUS_ZH[s] || s;
}

function accountDetail(r) {
  const parts = [statusZh(r)];
  if (r.reserve) parts.push("主账号（其他账号到上限才用）");
  if (r.schedule && r.schedule.length) parts.push(`时段 ${r.schedule.join(",")}`);
  return parts.filter(Boolean).join(" · ");
}

// Why a manual switch to this row would be undone by cswap auto, or null.
function switchBlockReason(r) {
  const n = `#${r.number}`;
  switch (rowState(r)) {
    case "disabled":
      return `${n} 已停用`;
    case "limit": {
      const w = r.blockedWindow || (String(r.status || "").match(/^Policy-limit \((.+)\)$/) || [])[1];
      return `${n} 已到${w ? ` ${w} ` : ""}上限`;
    }
    case "off-schedule":
      return `${n} 不在它的使用时段`;
    default:
      return null;
  }
}

// ---- auto-switch

const TRIGGER_ZH = {
  proactive: "提前切换",
  "at-limit": "到达限额",
  "policy-limit": "个人上限",
  failover: "故障转移",
  "reserve-return": "回到副账号",
  schedule: "时段",
  "consume-first": "先用完",
  manual: "手动",
};

const TRIGGER_HELP = {
  proactive: "当前账号用量到了自动切换的阈值，提前换到余量更多的账号。",
  "at-limit": "当前账号已经用满 Anthropic 的限额。",
  "policy-limit": "当前账号到了你给它设的个人上限（不是 Anthropic 的限额），不受冷却时间限制。",
  failover: "连续几次读不到当前账号的用量（token 失效或网络问题），换到能用的账号。",
  "reserve-return": "正在用主账号，副账号又能用了，切回副账号。",
  schedule: "另一个账号的使用时段开始了。",
  "consume-first": "先用完快要重置的账号，再用别的。",
  manual: "手动切换（cswap switch、侧栏 ⇄ 或手机）。",
};

const triggerZh = (t) => (t ? TRIGGER_ZH[t] || t : "");

const REASON_ZH = {
  "below-threshold": "低于阈值",
  "reserve-in-use": "正在用主账号",
  "reserve-hold": "停在主账号，等副账号可用",
  "below-personal-limit": "低于个人上限",
  cooldown: "冷却中",
  "manual-hold": "手动切换后保持",
  "stale-usage": "用量数据太旧",
  "active-idle": "当前账号空闲",
  "active-usage-unknown": "当前账号用量未知",
  "no-comparison": "无法比较",
  "policy-exhausted": "所有账号都到个人上限",
  "all-exhausted": "所有账号都用满了",
  "no-candidates": "没有可切换的账号",
  "no-viable-target": "没有合适的目标账号",
  "no-qualifying-candidate": "没有满足条件的账号",
  "scheduled-account-unavailable": "时段账号不可用",
  paused: "已暂停",
  error: "出错",
};

const reasonZh = (r) => (r ? REASON_ZH[r] || r : "");

const SOURCE_ZH = { own: "个人上限", threshold: "全局阈值", weekly: "每周阈值" };

// ---- time

const pad = (n) => String(n).padStart(2, "0");

function clock(epochSec) {
  const d = new Date(Number(epochSec) * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dayClock(epochSec) {
  const d = new Date(Number(epochSec) * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 90 -> "1 分钟", 7200 -> "2 小时", 2 days -> "2 天"
function span(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
  }
  return `${Math.round(s / 86400)} 天`;
}

// The short form under a usage ring: 2640 -> "44分", 6240 -> "1时44分",
// 2 days 3 hours -> "2天3时".
function shortSpan(sec) {
  const m = Math.max(0, Math.round((Number(sec) || 0) / 60));
  if (m < 1) return "马上";
  if (m < 60) return `${m}分`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}时${m % 60}分` : `${h}时`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}天${h % 24}时` : `${d}天`;
}

const ago = (epochSec, nowSec) => `${span((nowSec || Date.now() / 1000) - Number(epochSec))}前`;

// "3:12" for a countdown in seconds
function mmss(sec) {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

// ---- api-worker tasks

const API_DONE = new Set(["done", "failed", "cancelled", "lost", "closed"]);
// "launching": a cloud session that is still being started
const isRunning = (t) => t.state === "queued" || t.state === "running" || t.state === "launching";
// api-worker sends `final` per task; older ones only the state.
const isFinal = (t) => (typeof t.final === "boolean" ? t.final : API_DONE.has(t.state));

const TASK_STATE_ZH = {
  queued: "排队中",
  running: "运行中",
  launching: "启动中",
  done: "完成",
  failed: "失败",
  cancelled: "已停止",
  lost: "丢失",
  // a cloud session that cannot report back here (no git remote): only its page knows
  cloud: "已交给云端（无法跟踪）",
  closed: "已标记结束",
};
const taskStateZh = (s) => TASK_STATE_ZH[s] || s || "";

module.exports = {
  where,
  pct,
  usageText,
  usageBar,
  money,
  defaultCwd,
  rowState,
  isWarn,
  nearLimit,
  NEAR_MARGIN,
  compactUsage,
  shortSpan,
  statusZh,
  accountDetail,
  switchBlockReason,
  TRIGGER_ZH,
  TRIGGER_HELP,
  triggerZh,
  reasonZh,
  SOURCE_ZH,
  clock,
  dayClock,
  span,
  ago,
  mmss,
  isRunning,
  isFinal,
  taskStateZh,
};
