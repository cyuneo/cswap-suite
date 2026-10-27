// cswap-remote web UI (v0.2). Vanilla JS, no build step.
//
// Security notes: everything that comes from a transcript or a terminal is
// untrusted. It reaches the DOM only through textContent, or through
// CRmd.render() (static/md.js), which escapes first. No inline handlers or
// inline <script>/<style> (CSP: script-src 'self').
(function () {
  "use strict";

  // ================================================================ pure helpers
  // (no DOM; exported for tests/app_test.js when loaded under node)

  // Server-Sent Events framing for a fetch() body: feed decoded text chunks,
  // get back complete messages. Frames may be split across chunks and one
  // chunk may hold several frames.
  function SSEBuffer() { this.buf = ""; }
  SSEBuffer.prototype.push = function (chunk) {
    this.buf += chunk;
    const out = [];
    const re = /\r?\n\r?\n/;
    let m;
    while ((m = re.exec(this.buf))) {
      const frame = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      const msg = parseFrame(frame);
      if (msg) out.push(msg);
    }
    return out;
  };
  function parseFrame(frame) {
    const data = frame.split(/\r?\n/).filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, "")).join("\n");
    if (!data) return null; // comment / keep-alive
    try {
      const v = JSON.parse(data);
      return v && typeof v === "object" ? v : null;
    } catch (e) { return null; }
  }

  // "diff --git" text -> [{path, meta:[...], hunks:[{oldStart,oldLines,newStart,newLines,lines}]}]
  function parseUnifiedDiff(text) {
    const files = [];
    let f = null, hk = null;
    for (const l of String(text || "").split("\n")) {
      if (l.startsWith("diff --git ")) {
        const m = / b\/(.*)$/.exec(l);
        f = { path: m ? m[1] : l.slice(11), meta: [], hunks: [] };
        files.push(f); hk = null;
        continue;
      }
      if (!f) continue;
      const m = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(l);
      if (m) {
        hk = { oldStart: +m[1], oldLines: m[2] == null ? 1 : +m[2], newStart: +m[3], newLines: m[4] == null ? 1 : +m[4], lines: [] };
        f.hunks.push(hk);
        continue;
      }
      if (hk && /^[-+ \\]/.test(l)) hk.lines.push(l);
      else if (!hk && l && !l.startsWith("index ") && !l.startsWith("--- ") && !l.startsWith("+++ ")) f.meta.push(l);
    }
    return files;
  }

  // Patch from an Edit/Write/MultiEdit tool_use input (used before the real
  // structuredPatch arrives, and on the permission card).
  function inputPatch(name, input) {
    input = input || {};
    const lines = (s, p) => String(s).split("\n").map((l) => p + l);
    if (name === "Write" && typeof input.content === "string") {
      const ls = lines(input.content, "+");
      return [{ oldStart: 0, oldLines: 0, newStart: 1, newLines: ls.length, lines: ls }];
    }
    const edits = Array.isArray(input.edits) ? input.edits : [input];
    return edits.filter((x) => x && (typeof x.old_string === "string" || typeof x.new_string === "string")).map((x) => {
      const ls = (x.old_string ? lines(x.old_string, "-") : []).concat(x.new_string ? lines(x.new_string, "+") : []);
      return { oldStart: null, oldLines: 0, newStart: null, newLines: 0, lines: ls };
    });
  }
  function patchCounts(patch) {
    let a = 0, r = 0;
    for (const hk of patch || []) for (const l of hk.lines || []) {
      if (l[0] === "+") a++; else if (l[0] === "-") r++;
    }
    return { a, r };
  }

  // Reconnect delay after `failures` failed attempts in a row: 1 s, 2 s, 5 s, 10 s.
  // (0 failures: the stream had worked and just closed; retry after 1 s.)
  function backoffDelay(failures) {
    const DELAYS = [1000, 2000, 5000, 10000];
    return DELAYS[Math.min(Math.max(failures - 1, 0), DELAYS.length - 1)];
  }

  function fmtElapsed(sec) {
    sec = Math.max(0, Math.floor(sec));
    if (sec < 60) return sec + " 秒";
    if (sec < 3600) return Math.floor(sec / 60) + " 分 " + String(sec % 60).padStart(2, "0") + " 秒";
    return Math.floor(sec / 3600) + " 小时 " + Math.floor((sec % 3600) / 60) + " 分";
  }
  function ago(ms, now) {
    if (!ms) return "";
    const s = Math.max(0, ((now || Date.now()) - ms) / 1000);
    if (s < 5) return "刚刚";
    if (s < 60) return Math.floor(s) + " 秒前";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }
  function clipLines(text, n) {
    const ls = String(text || "").split("\n");
    return ls.length > n ? { text: ls.slice(0, n).join("\n"), more: ls.length - n } : { text: String(text || ""), more: 0 };
  }

  // Which consecutive events fold together. Returns a list of segments:
  // {kind:"one", ev} | {kind:"think", evs} | {kind:"fold", evs, tools}
  const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"]);
  const LINE_TOOLS = new Set(["Read", "Glob", "Grep", "LS", "TodoWrite", "NotebookRead"]);
  const AGENT_TOOLS = new Set(["Task", "Agent"]);
  const foldable = (e) => e.kind === "tool" && e.status === "ok" && !EDIT_TOOLS.has(e.name) && !AGENT_TOOLS.has(e.name);
  function segment(evs) {
    const out = [];
    let buf = [];
    const plain = (list) => {
      let think = null;
      for (const e of list) {
        if (e.kind === "thinking") {
          if (!think) { think = { kind: "think", evs: [] }; out.push(think); }
          think.evs.push(e);
        } else { think = null; out.push({ kind: "one", ev: e }); }
      }
    };
    const flush = () => {
      const tools = buf.filter((e) => e.kind === "tool");
      if (tools.length >= 3) out.push({ kind: "fold", evs: buf, tools });
      else plain(buf);
      buf = [];
    };
    for (const e of evs) {
      if (e.kind === "thinking" || foldable(e)) { buf.push(e); continue; }
      flush();
      plain([e]);
    }
    flush();
    return out;
  }

  // Two versions of one event that would render the same (a reconnect or a
  // poll brings freshly parsed copies of unchanged events).
  function sameEvent(a, b) {
    if (a === b) return true;
    if (!a || !b) return false;
    try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
  }

  // What to do with a snapshot's events, given the session id of the
  // previous snapshot: "replace" the timeline, or "keep" it and add a
  // divider (claude exited, or a new claude has no events yet).
  function snapshotPlan(prevSid, session, events) {
    const sid = session ? session.sessionId || null : null;
    if (prevSid && !(events && events.length) && sid !== prevSid) return sid ? "keep-new" : "keep-exit";
    return "replace";
  }

  // `cswap policy list --json` rows. Newer cswap sends a machine `state`;
  // older ones only the English status text, mapped here as a fallback.
  function accountState(a) {
    if (a.state) return a.state;
    const s = String(a.status || "");
    if (a.enabled === false || s === "Disabled") return "disabled";
    if (s.startsWith("Policy-limit")) return "limit";
    if (s === "Off-schedule") return "off-schedule";
    if (s === "Usage unknown") return "unknown";
    if (s === "Not managed") return "unmanaged";
    if (s === "Reserve") return "reserve";
    return a.active ? "active" : "ready";
  }
  const ACCOUNT_STATE_ZH = { active: "当前", ready: "可用", disabled: "已停用", limit: "到上限", "off-schedule": "不在时段",
    reserve: "主账号", unknown: "用量未知", unmanaged: "未管理" };
  function limitWindow(a) {
    return a.blockedWindow || (String(a.status || "").match(/^Policy-limit \((.+)\)$/) || [])[1] || null;
  }
  function accountLabel(a) {
    const st = accountState(a);
    if (st === "limit") { const w = limitWindow(a); return w ? `到上限（${w}）` : ACCOUNT_STATE_ZH.limit; }
    return ACCOUNT_STATE_ZH[st] || a.status || st;
  }
  // Why cswap auto would undo a manual switch to this account, or null.
  function switchWarning(a) {
    const n = `账号 ${a.number}`;
    switch (accountState(a)) {
      case "disabled": return `${n} 已停用`;
      case "limit": { const w = limitWindow(a); return `${n} 已到${w ? " " + w + " " : ""}上限`; }
      case "off-schedule": return `${n} 不在它的使用时段`;
      default: return null;
    }
  }
  function switchConfirmText(a) {
    const why = switchWarning(a);
    if (!why) return `切换到账号 ${a.number}？正在运行的会话会从下一次请求开始使用这个账号。`;
    return `${why}${a.reason ? "（" + a.reason + "）" : ""}。自动切换会在约 1 分钟内切回别的账号，` +
      "手动切过去多半留不住。仍然切换？";
  }

  if (typeof window === "undefined") {
    module.exports = { SSEBuffer, parseFrame, parseUnifiedDiff, inputPatch, patchCounts, backoffDelay, fmtElapsed, ago, clipLines, segment,
      sameEvent, snapshotPlan, accountState, accountLabel, switchWarning, switchConfirmText };
    return;
  }

  // ================================================================ DOM basics
  const $ = (s, el) => (el || document).querySelector(s);
  const md = window.CRmd;
  const main = $("#main");

  // h("div", {class, text, onclick, ...}, ...children). Strings become text nodes.
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "html") el.innerHTML = v; // only md.render() output or literals
        else if (k.startsWith("on")) el[k] = v;
        else if (k === "value") el.value = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach((x) => append(el, x)); return; }
    el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  // Replace an element's children; accepts nodes, strings, arrays and nulls.
  function fill(el) {
    el.replaceChildren();
    for (let i = 1; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  // Put `items` into `parent` in order, reusing nodes already there.
  function reconcile(parent, items) {
    let cur = parent.firstChild;
    for (const n of items) {
      if (n === cur) { cur = cur.nextSibling; continue; }
      parent.insertBefore(n, cur);
    }
    while (cur) { const next = cur.nextSibling; parent.removeChild(cur); cur = next; }
  }

  let token = "";
  try { token = localStorage.getItem("cr_token") || ""; } catch (e) {}
  const tm = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (tm) {
    token = tm[1];
    try { localStorage.setItem("cr_token", token); } catch (e) {}
    history.replaceState(null, "", location.pathname);
  }

  function toast(msg, ms) {
    const t = h("div", { class: "toast", text: msg });
    document.body.appendChild(t);
    setTimeout(() => t.remove(), ms || 2200);
  }
  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set(k, v) { try { v ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); } catch (e) {} },
  };
  async function copyText(t) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(t); toast("已复制", 1000); return; }
    } catch (e) { /* fall through */ }
    const ta = h("textarea", { readonly: true });
    ta.value = t;
    ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    ta.remove();
    toast(ok ? "已复制" : "复制失败，请长按选择文字", 1500);
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest && ev.target.closest(".md-copy");
    if (!b) return;
    const pre = b.closest(".md-code") && b.closest(".md-code").querySelector("pre");
    if (pre) copyText(pre.textContent);
  });

  // Every screen gets a generation number; responses that arrive after the
  // user moved to another screen are dropped instead of drawn.
  let view = 0, ctrl = new AbortController(), timers = [], cleanups = [], refreshNow = null;
  function newView() {
    view++; ctrl.abort(); ctrl = new AbortController();
    timers.forEach(clearInterval); timers = [];
    cleanups.forEach((f) => { try { f(); } catch (e) {} }); cleanups = [];
    refreshNow = null; window.onscroll = null;
    closeSheet();
    setDock(null);
    $("#newPill").classList.add("hidden");
    setMenu(null);
    return view;
  }
  const alive = (g) => g === view;
  function every(ms, fn) {
    refreshNow = fn; fn();
    timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms));
  }

  async function api(path, opts) {
    opts = Object.assign({}, opts || {});
    const headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
    if (opts.body && typeof opts.body !== "string") { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.body); }
    const r = await fetch("/api/" + path, Object.assign({}, opts, { headers, cache: "no-store", signal: opts.signal || ctrl.signal }));
    if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(data.error || ("HTTP " + r.status)); e.status = r.status; throw e; }
    return data;
  }
  const quiet = (e) => e.name === "AbortError" || e.message === "unauthorized";
  const fail = (e, ms) => { if (!quiet(e)) toast(e.message, ms || 3500); };

  const STATUS = { waiting: "等你处理", busy: "运行中", idle: "空闲", starting: "启动中", shell: "未运行 claude", unknown: "未知" };
  // Claude Code's waitingFor reasons, in Chinese (unknown ones pass through).
  const WAIT_ZH = {
    "permission prompt": "需要你批准", "dialog open": "打开了一个对话框", "input needed": "需要你输入",
    "worker request": "子任务请求", "sandbox request": "沙箱权限请求", "goal proposal": "目标提议",
  };
  const waitText = (w) => WAIT_ZH[String(w || "").trim().toLowerCase()] || w;
  const KEYNAME = { Enter: "回车", Escape: "Esc", Up: "↑", Down: "↓", BTab: "⇧Tab", "C-c": "Ctrl-C" };
  const MODES = [
    ["default", "默认", "每次修改文件、执行命令都先问你"],
    ["acceptEdits", "自动接受编辑", "修改文件不再询问，执行命令仍会问"],
    ["plan", "计划", "只读和规划，不改文件"],
    ["auto", "自动", "由 Claude 判断哪些操作需要询问"],
  ];
  const MODE_NAME = { default: "默认", acceptEdits: "自动接受编辑", plan: "计划", auto: "自动", bypassPermissions: "跳过权限" };
  const pct = (v) => (v === null || v === undefined ? "?" : Math.round(v));
  const roTag = (s) => (s.source === "claude-vscode" ? "只读 · VS Code" : "只读 · 终端");
  const go = (hash) => { location.hash = hash; };
  const sessHash = (key, extra) => "s=" + encodeURIComponent(key) + (extra ? "&" + extra : "");
  function shortPath(p) {
    const parts = String(p || "").split("/").filter(Boolean);
    return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : String(p || "");
  }
  function rel(p, cwd) {
    p = String(p || "");
    if (cwd && p === cwd) return "";
    return cwd && p.startsWith(cwd + "/") ? p.slice(cwd.length + 1) : p;
  }
  const baseName = (p) => String(p || "").split("/").filter(Boolean).pop() || String(p || "");
  const tsMs = (ts) => (typeof ts === "number" ? (ts < 1e12 ? ts * 1000 : ts) : (ts ? Date.parse(ts) || null : null));

  // ================================================================ chrome: header, menu, sheet, dock
  let backHash = null;
  function setHeader(title, back, sub) {
    $("#title").textContent = title;
    backHash = back;
    $("#back").classList.toggle("hidden", back == null);
    const s = $("#headSub");
    s.textContent = sub || "";
    s.classList.toggle("hidden", !sub);
    document.title = title === "cswap Remote" ? title : title + " · cswap Remote";
  }
  $("#back").onclick = () => { go(backHash || ""); };
  $("#acct").onclick = () => { go("accounts"); };
  function setMenu(fn) {
    const b = $("#menuBtn");
    b.classList.toggle("hidden", !fn);
    b.onclick = fn;
  }

  let sheetOnClose = null;
  function openSheet(title, kids, onClose) {
    const sh = $("#sheet");
    sh.replaceChildren();
    if (title) sh.appendChild(h("h2", { text: title }));
    append(sh, kids);
    $("#sheetWrap").classList.remove("hidden");
    sheetOnClose = onClose || null;
    return sh;
  }
  function closeSheet() {
    const w = $("#sheetWrap");
    if (w.classList.contains("hidden")) return;
    w.classList.add("hidden");
    $("#sheet").replaceChildren();
    const f = sheetOnClose; sheetOnClose = null;
    if (f) f();
  }
  const sheetOpen = () => !$("#sheetWrap").classList.contains("hidden");
  $("#sheetBackdrop").onclick = closeSheet;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && sheetOpen()) closeSheet(); });

  // The fixed bottom dock. Its height feeds --dock-h (body padding), the
  // on-screen keyboard height feeds --kb (iOS does not shrink the layout
  // viewport, so the dock is lifted by hand).
  const dock = $("#dock");
  function setDock(content) {
    dock.replaceChildren();
    if (content) dock.appendChild(content);
    dock.classList.toggle("hidden", !content);
    document.body.classList.toggle("has-dock", !!content);
    syncDock();
  }
  function syncDock() {
    const hgt = dock.classList.contains("hidden") ? 0 : dock.offsetHeight;
    const v = hgt + "px", root = document.documentElement;
    if (root.style.getPropertyValue("--dock-h") === v) return;
    // keep the bottom of the page in view when the dock grows or shrinks
    const near = window.innerHeight + window.scrollY >= root.scrollHeight - 160;
    root.style.setProperty("--dock-h", v);
    if (near && hgt) window.scrollTo(0, root.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(syncDock).observe(dock);
  function syncKeyboard() {
    const vv = window.visualViewport;
    if (!vv) return;
    const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    document.documentElement.style.setProperty("--kb", (kb > 40 ? kb : 0) + "px");
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncKeyboard);
    window.visualViewport.addEventListener("scroll", syncKeyboard);
  }

  // ================================================================ routing
  function parseHash() {
    const out = {};
    for (const part of location.hash.slice(1).split("&")) {
      if (!part) continue;
      const i = part.indexOf("=");
      const k = i < 0 ? part : part.slice(0, i);
      let v = i < 0 ? "" : part.slice(i + 1);
      try { v = decodeURIComponent(v); } catch (e) {}
      out[k] = v;
    }
    return out;
  }
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && refreshNow) refreshNow(true); });

  function route() {
    const g = newView();
    if (!token) return showLogin();
    const p = parseHash();
    refreshAccountChip();
    if (p.s != null) {
      if (p.v === "term") return showTerm(g, p.s);
      if (p.v === "git") return showGit(g, p.s);
      if (p.agent) return showAgent(g, p.s, p.agent);
      return showSession(g, p.s);
    }
    if ("accounts" in p) return showAccounts(g);
    showList(g);
  }

  function showLogin() {
    newView();
    setHeader("cswap Remote", null);
    $("#acct").classList.add("hidden");
    const inp = h("input", { class: "text-input", autocomplete: "off", autocapitalize: "off" });
    const ok = h("button", { class: "primary", type: "button", text: "登录" });
    ok.onclick = () => {
      token = inp.value.trim();
      try { localStorage.setItem("cr_token", token); } catch (e) {}
      route();
    };
    main.replaceChildren(h("div", { class: "card" },
      h("b", { text: "登录" }),
      h("p", { class: "small" }, "在服务器上运行 ", h("code", { text: "cswap-remote url" }),
        "，用手机打开它输出的链接即可（链接里带访问密钥）。也可以把密钥粘贴到这里："),
      inp,
      h("div", { class: "row end mt" }, ok),
      h("p", { class: "small", text: "提示：iPhone 上“添加到主屏幕”后的 App 和 Safari 不共享登录，第一次从主屏幕打开时需要再粘贴一次密钥。" })));
  }

  // ================================================================ account chip
  let chipAt = 0;
  async function refreshAccountChip() {
    if (Date.now() - chipAt < 30000) return;
    chipAt = Date.now();
    try {
      const d = await api("accounts", { signal: AbortSignal.timeout ? AbortSignal.timeout(40000) : undefined });
      const a = (d.accounts || []).find((x) => x.active);
      const chip = $("#acct");
      chip.textContent = a ? `${a.name.split("@")[0]} · 5h ${pct(a.usage5h)}%` : "账号";
      chip.classList.remove("hidden");
    } catch (e) { chipAt = 0; /* cswap may be absent */ }
  }

  // ================================================================ session list
  function showList(g) {
    setHeader("Claude 会话", null);
    const info = h("div", { class: "grow small" });
    const list = h("div", {}, h("div", { class: "empty", text: "加载中…" }));
    const recent = h("div", { class: "recent" });
    let showAll = false;
    const loadRecent = async () => {
      try {
        const d = await api("recent");
        if (!alive(g)) return;
        const all = d.conversations || [];
        const shown = showAll ? all : all.slice(0, 6);
        fill(recent,
          all.length ? h("div", { class: "section", text: "最近的对话（已关闭，可在手机上继续）" }) : null,
          shown.map((c) => h("div", { class: "card crow" },
            h("div", { class: "grow" },
              h("div", { class: "title", text: c.title || "（无标题）" }),
              h("div", { class: "sub", text: shortPath(c.cwd) + " · " + ago(c.updatedAt) })),
            c.cwdExists ? h("button", { type: "button", text: "继续", onclick: () => resumeConversation(c.sessionId, c.title) })
              : h("span", { class: "small", text: "目录已不存在" }))),
          all.length > shown.length ? h("button", { type: "button", class: "link", text: `显示全部 ${all.length} 个`,
            onclick: () => { showAll = true; loadRecent(); } }) : null);
      } catch (e) { /* optional section */ }
    };
    loadRecent();
    timers.push(setInterval(() => { if (!document.hidden) loadRecent(); }, 30000));
    main.replaceChildren(
      h("div", { class: "row list-top" }, info, h("button", { class: "primary", type: "button", text: "＋ 新会话", onclick: openNew })),
      list, recent);
    api("status").then((s) => {
      if (alive(g)) info.textContent = s.host + (s.tmux ? "" : " · 未安装 tmux") + (s.notify ? " · 推送已开" : "");
    }).catch(() => {});
    every(3000, async () => {
      try {
        const d = await api("sessions");
        if (alive(g)) renderList(list, d.sessions || []);
      } catch (e) {
        if (!quiet(e) && alive(g)) list.replaceChildren(h("div", { class: "empty", text: e.message }));
      }
    });
  }
  async function resumeConversation(sessionId, title) {
    if (!confirm(`在手机上继续「${title || "这个对话"}」？\n会在 tmux 里运行 claude --resume，之后电脑上也可以用 tmux attach 接着用。`)) return;
    try {
      const r = await api("resume", { method: "POST", body: { sessionId } });
      toast("已在 tmux 里继续这个对话");
      go(sessHash(r.key));
    } catch (e) { if (!quiet(e)) toast(e.message, 6000); }
  }
  function renderList(el, list) {
    if (!list.length) {
      el.replaceChildren(h("div", { class: "empty", text: "没有正在运行的 Claude 会话。点“新会话”在 tmux 里启动一个。" }));
      return;
    }
    const waiting = list.filter((s) => s.status === "waiting");
    const others = list.filter((s) => s.status !== "waiting");
    fill(el,
      waiting.length ? h("div", { class: "section attn", text: "需要你处理" }) : null,
      waiting.map(sessionRow),
      others.length && waiting.length ? h("div", { class: "section", text: "其他会话" }) : null,
      others.map(sessionRow));
  }
  function sessionRow(s) {
    // The title gets the full width (up to two lines); time sits on its
    // right and the tags go to their own line, so long titles are not cut
    // down to a few characters.
    const tags = [
      s.agentsRunning > 0 ? h("span", { class: "tag agents", text: "🤖 " + s.agentsRunning + " 个运行中" }) : null,
      s.controllable ? null : h("span", { class: "tag", text: roTag(s) }),
    ].filter(Boolean);
    const where = shortPath(s.cwd) + (s.tmux ? " · tmux " + s.tmux : "");
    return h("button", { type: "button", class: "card srow", onclick: () => go(sessHash(s.key)) },
      h("span", { class: "dot " + s.status, title: STATUS[s.status] || s.status }),
      h("div", { class: "grow" },
        h("div", { class: "trow" },
          h("div", { class: "title", text: s.title || s.name }),
          h("span", { class: "time", text: ago(s.updatedAt) })),
        s.status === "waiting" && s.waitingFor ? h("div", { class: "wait", text: waitText(s.waitingFor) }) : null,
        h("div", { class: "sub", text: where }),
        tags.length ? h("div", { class: "tags" }, tags) : null));
  }

  async function openNew() {
    const dlg = $("#newDlg");
    const sel = $("#nDir");
    sel.replaceChildren(h("option", { value: "", text: "加载中…" }));
    $("#nDirText").value = ""; $("#nName").value = ""; $("#nResume").checked = false;
    dlg.showModal();
    try {
      const d = await api("projects");
      const opts = (d.projects || []).map((p) => h("option", { value: p.path, text: p.label }));
      sel.replaceChildren.apply(sel, opts.length ? opts : [h("option", { value: "", text: "（没有已知目录，请在下面输入）" })]);
    } catch (e) { if (!quiet(e)) sel.replaceChildren(h("option", { value: "", text: e.message })); }
  }
  $("#nCancel").onclick = () => $("#newDlg").close();
  $("#newForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const cwd = $("#nDirText").value.trim() || $("#nDir").value;
    if (!cwd) return toast("请选择或输入目录");
    $("#nOk").disabled = true;
    try {
      const r = await api("sessions", { method: "POST", body: { cwd, name: $("#nName").value.trim(), resume: $("#nResume").checked } });
      $("#newDlg").close();
      toast("已启动。第一次进入新目录时 claude 会问是否信任该目录", 3500);
      go(sessHash(r.key));
    } catch (e) { fail(e); } finally { $("#nOk").disabled = false; }
  };

  // ================================================================ diff component
  function renderPatch(patch, maxLines) {
    const box = h("div", { class: "diff" });
    let shown = 0, hidden = 0;
    const limit = maxLines || 400;
    for (const hk of patch || []) {
      if (hk.oldStart != null && shown < limit) {
        box.appendChild(h("div", { class: "dl hunk", text: `@@ -${hk.oldStart},${hk.oldLines} +${hk.newStart},${hk.newLines} @@` }));
      }
      for (const l of hk.lines || []) {
        if (shown >= limit) { hidden++; continue; }
        const c = l[0] === "+" ? " add" : l[0] === "-" ? " del" : "";
        box.appendChild(h("div", { class: "dl" + c, text: l || " " }));
        shown++;
      }
    }
    if (hidden) box.appendChild(h("div", { class: "dl more", text: `… 还有 ${hidden} 行` }));
    return box;
  }
  function countsBadge(patch) {
    const c = patchCounts(patch);
    return h("span", { class: "counts" }, h("span", { class: "a", text: "+" + c.a }), h("span", { class: "r", text: "−" + c.r }));
  }

  // ================================================================ event rendering
  // ctx: {k, cwd(), findAgent(e), agentHash(id)}
  function toolSubtitle(e, cwd) {
    const i = e.input || {};
    switch (e.name) {
      case "Bash": return i.description || i.command || "";
      case "Read": case "Edit": case "Write": case "MultiEdit": return rel(i.file_path, cwd);
      case "NotebookEdit": case "NotebookRead": return rel(i.notebook_path, cwd);
      case "Glob": case "Grep": { const d = rel(i.path, cwd); return (i.pattern || "") + (d ? " · " + d : ""); }
      case "LS": return rel(i.path, cwd) || ".";
      case "TodoWrite": return "更新任务清单";
      case "WebFetch": return i.url || "";
      case "WebSearch": return i.query || "";
      case "Task": case "Agent": return i.description || i.subagent_type || "";
      default: {
        for (const k of Object.keys(i)) if (typeof i[k] === "string" && i[k]) return i[k].split("\n")[0];
        return "";
      }
    }
  }
  const toolLabel = (e) => (AGENT_TOOLS.has(e.name) ? "智能体" : e.name);

  // Output block with a 30-line preview and "显示全部" (fetches the full text).
  // What the user expanded is remembered per tool call and block (ctx.expanded),
  // so a card that is rebuilt (new result, reconnect) stays expanded.
  function outBlock(text, cls, ctx, id, truncated) {
    const ek = id + "|" + (cls || "out");
    const full = ctx.expanded && ctx.expanded.get(ek);
    if (full != null) return h("div", {}, h("pre", { class: cls || "", text: full }));
    const c = clipLines(text, 30);
    const pre = h("pre", { class: cls || "", text: c.text });
    const wrap = h("div", {}, pre);
    const keep = (t) => { if (ctx.expanded) ctx.expanded.set(ek, t); };
    if (c.more || truncated) {
      const b = h("button", { type: "button", class: "link", text: c.more ? `显示全部（还有 ${c.more} 行）` : "显示全部" });
      b.onclick = async () => {
        if (!truncated) { pre.textContent = text; keep(text); b.remove(); return; }
        b.disabled = true; b.textContent = "读取中…";
        try {
          const r = await api(`session/${ctx.k}/toolresult/${encodeURIComponent(id)}`);
          pre.textContent = r.text || ""; keep(r.text || ""); b.remove();
        } catch (e) { fail(e); b.disabled = false; b.textContent = "显示全部"; }
      };
      wrap.appendChild(b);
    }
    return wrap;
  }
  function ioLabel(label, copy) {
    return h("div", { class: "io-label" }, h("span", { text: label }),
      copy != null ? h("button", { type: "button", class: "copy-btn", text: "复制", onclick: () => copyText(copy) }) : null);
  }

  function toolBody(e, ctx) {
    const r = e.result, i = e.input || {};
    const body = h("div", { class: "io" });
    if (EDIT_TOOLS.has(e.name)) {
      const patch = r && r.patch && r.patch.length ? r.patch : inputPatch(e.name, i);
      if (patch.length) body.appendChild(renderPatch(patch));
      if (r && e.status === "error" && r.text) body.appendChild(outBlock(r.text, "stderr", ctx, e.id, r.truncated));
      return body;
    }
    if (e.name === "Bash") {
      body.appendChild(ioLabel("IN", i.command || ""));
      body.appendChild(h("pre", { text: i.command || "" }));
      body.appendChild(ioLabel("OUT"));
      if (!r) { body.appendChild(h("div", { class: "small", text: "运行中…" })); return body; }
      const out = r.stdout != null || r.stderr != null ? r.stdout : r.text;
      if (out) body.appendChild(outBlock(out, "", ctx, e.id, r.truncated));
      if (r.stderr) body.appendChild(outBlock(r.stderr, "stderr", ctx, e.id, r.truncated));
      if (!out && !r.stderr) body.appendChild(h("div", { class: "small", text: r.text || "（无输出）" }));
      if (r.interrupted) body.appendChild(h("div", { class: "small err-text", text: "已中断" }));
      return body;
    }
    if (AGENT_TOOLS.has(e.name)) {
      if (i.prompt) { body.appendChild(ioLabel("任务")); body.appendChild(h("div", { class: "md", html: md.render(i.prompt) })); }
      if (r && r.text) { body.appendChild(ioLabel("结果")); body.appendChild(outBlock(r.text, e.status === "error" ? "stderr" : "", ctx, e.id, r.truncated)); }
      return body;
    }
    const inText = JSON.stringify(i, null, 2);
    if (inText && inText !== "{}") { body.appendChild(ioLabel("IN")); body.appendChild(outBlock(inText, "", ctx, e.id, false)); }
    body.appendChild(ioLabel("OUT"));
    if (!r) body.appendChild(h("div", { class: "small", text: "运行中…" }));
    else body.appendChild(outBlock(r.text || "（无输出）", e.status === "error" ? "stderr" : "", ctx, e.id, r.truncated));
    return body;
  }

  function renderTool(e, ctx, openState) {
    const err = e.status === "error";
    const isEdit = EDIT_TOOLS.has(e.name);
    const line = LINE_TOOLS.has(e.name) && !err;
    const card = h("div", { class: "ev tool" + (err ? " err" : "") + (line ? " line" : "") });
    const kids = [h("span", { class: "dot " + (e.status || "running") }), h("b", { text: toolLabel(e) }),
      h("span", { class: "tsub", text: toolSubtitle(e, ctx.cwd()) })];
    if (isEdit) {
      const patch = e.result && e.result.patch && e.result.patch.length ? e.result.patch : inputPatch(e.name, e.input);
      kids.push(countsBadge(patch));
    }
    if (line) { card.appendChild(h("div", { class: "tool-h" }, kids)); return card; }
    kids.push(h("span", { class: "chev", text: "›" }));
    const head = h("button", { type: "button", class: "tool-h" }, kids);
    card.appendChild(head);
    if (AGENT_TOOLS.has(e.name)) {
      const a = ctx.findAgent(e);
      const st = a ? a.status : e.status === "running" ? "running" : e.status === "error" ? "failed" : "done";
      const stText = { running: "运行中", done: "已完成", failed: "失败或已中断" }[st] || st;
      card.appendChild(h("div", { class: "agent-line" }, h("span", { class: "dot " + st }),
        h("span", { class: "grow sub", text: stText + (a && a.lastAction && st === "running" ? " · " + a.lastAction : "") }),
        a ? h("button", { type: "button", class: "link", text: "查看", onclick: () => go(ctx.agentHash(a.id)) }) : null));
    }
    const bodyWrap = h("div", { class: "tool-b" });
    card.appendChild(bodyWrap);
    let built = false;
    const setOpen = (on) => {
      card.classList.toggle("open", on);
      if (on && !built) { built = true; bodyWrap.appendChild(toolBody(e, ctx)); }
    };
    setOpen(openState.has(e.id) ? openState.get(e.id) : isEdit || err);
    head.onclick = () => { const on = !card.classList.contains("open"); openState.set(e.id, on); setOpen(on); };
    return card;
  }

  function renderEvent(e, ctx, openState) {
    switch (e.kind) {
      case "user": return h("div", { class: "ev ev-user" }, h("div", { class: "bubble", text: e.text || "" }));
      case "text": return e.text ? h("div", { class: "ev md", html: md.render(e.text) }) : null;
      case "system": return h("div", { class: "ev ev-system" }, h("span", { text: e.text || "" }));
      case "queued": return e.done ? null : h("div", { class: "ev ev-queued" }, h("span", { class: "qchip", text: "排队中：" + (e.text || "") }));
      case "tool": return renderTool(e, ctx, openState);
      default: return null;
    }
  }

  // Timeline: events by id, DOM nodes cached per event object; layout()
  // only moves nodes around, so a patch re-renders one card, not the list.
  function Timeline(el, ctx) {
    this.el = el; this.ctx = ctx;
    this.map = new Map(); this.order = [];
    this.nodes = new Map();  // id -> {ev, extra, node}
    this.groups = new Map(); // key -> {node, head, body}
    this.open = new Map();   // tool id -> user-chosen open state
    this.scheduled = false;
    this.hooks = { before: null, after: null };
  }
  // Unchanged events keep their old object, so their cached card (and
  // whatever the user expanded in it) is reused instead of rebuilt.
  Timeline.prototype.keepSame = function (id, ev) {
    const old = this.map.get(id);
    return old && sameEvent(old, ev) ? old : ev;
  };
  Timeline.prototype.upsert = function (ev) {
    if (!ev || ev.id == null) return;
    const id = String(ev.id);
    if (!this.map.has(id)) this.order.push(id);
    else if (this.keepSame(id, ev) !== ev) return;
    this.map.set(id, ev);
    this.schedule();
  };
  Timeline.prototype.prepend = function (evs) {
    const ids = [];
    for (const ev of evs || []) {
      if (!ev || ev.id == null) continue;
      const id = String(ev.id);
      if (!this.map.has(id)) ids.push(id);
      this.map.set(id, this.keepSame(id, ev));
    }
    this.order = ids.concat(this.order);
    this.render();
  };
  // A local, client-only line (e.g. "claude 已退出") after the last event.
  Timeline.prototype.divider = function (text) {
    const last = this.map.get(this.order[this.order.length - 1]);
    if (last && last.kind === "system" && last.local && last.text === text) return;
    this.upsert({ id: "local:" + Date.now() + ":" + this.order.length, kind: "system", local: true, text });
  };
  // Snapshot semantics: evs are the newest events. Older events we already
  // hold are kept if the snapshot connects to them. Returns true if kept.
  Timeline.prototype.replaceTail = function (evs) {
    evs = (evs || []).filter((e) => e && e.id != null);
    const ids = evs.map((e) => String(e.id));
    const pos = ids.length ? this.order.indexOf(ids[0]) : -1;
    const keep = pos > 0 ? this.order.slice(0, pos) : [];
    const map = new Map();
    for (const id of keep) map.set(id, this.map.get(id));
    for (const e of evs) map.set(String(e.id), this.keepSame(String(e.id), e));
    this.map = map;
    this.order = keep.concat(ids.filter((id, i) => ids.indexOf(id) === i));
    this.render();
    return keep.length > 0;
  };
  Timeline.prototype.schedule = function () {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(() => { this.scheduled = false; this.render(); });
  };
  Timeline.prototype.nodeFor = function (e) {
    const id = String(e.id);
    const extra = e.kind === "tool" && AGENT_TOOLS.has(e.name) ? JSON.stringify(this.ctx.findAgent(e) || null) : "";
    const c = this.nodes.get(id);
    if (c && c.ev === e && c.extra === extra) return c.node;
    const node = renderEvent(e, this.ctx, this.open);
    this.nodes.set(id, { ev: e, extra, node });
    return node;
  };
  Timeline.prototype.render = function () {
    if (this.hooks.before) this.hooks.before();
    const evs = this.order.map((id) => this.map.get(id)).filter(Boolean);
    const used = new Set(), usedGroups = new Set();
    const nodesOf = (segs) => {
      const out = [];
      for (const s of segs) {
        if (s.kind === "one") {
          used.add(String(s.ev.id));
          const n = this.nodeFor(s.ev);
          if (n) out.push(n);
        } else if (s.kind === "think") {
          const key = "think:" + s.evs[0].id;
          usedGroups.add(key);
          let gr = this.groups.get(key);
          if (!gr) { gr = { node: h("div", { class: "ev-thinking", text: "思考…" }) }; this.groups.set(key, gr); }
          out.push(gr.node);
        } else {
          const key = "fold:" + s.evs[0].id;
          usedGroups.add(key);
          let gr = this.groups.get(key);
          if (!gr) {
            const node = h("div", { class: "fold" });
            const head = h("button", { type: "button", class: "fold-h" });
            const body = h("div", { class: "fold-b" });
            head.onclick = () => node.classList.toggle("open");
            node.append(head, body);
            gr = { node, head, body };
            this.groups.set(key, gr);
          }
          const fails = s.tools.filter((t) => t.status === "error").length;
          const label = `${s.tools.length} 次工具调用` + (fails ? ` · ${fails} 失败` : "");
          if (gr.head.textContent !== "▸ " + label) gr.head.textContent = "▸ " + label;
          const inner = [];
          let think = null;
          for (const e of s.evs) {
            if (e.kind === "thinking") { if (!think) { think = { kind: "think", evs: [] }; inner.push(think); } think.evs.push(e); }
            else { think = null; inner.push({ kind: "one", ev: e }); }
          }
          reconcile(gr.body, nodesOf(inner));
          out.push(gr.node);
        }
      }
      return out;
    };
    reconcile(this.el, nodesOf(segment(evs)));
    for (const id of this.nodes.keys()) if (!used.has(id)) this.nodes.delete(id);
    for (const key of this.groups.keys()) if (!usedGroups.has(key)) this.groups.delete(key);
    if (this.hooks.after) this.hooks.after();
  };
  Timeline.prototype.lastRunningTool = function () {
    for (let i = this.order.length - 1, n = 0; i >= 0 && n < 60; i--, n++) {
      const e = this.map.get(this.order[i]);
      if (e && e.kind === "tool" && e.status === "running") return e;
      if (e && e.kind === "user") return null;
    }
    return null;
  };
  Timeline.prototype.lastUserTs = function () {
    for (let i = this.order.length - 1; i >= 0; i--) {
      const e = this.map.get(this.order[i]);
      if (e && e.kind === "user") return tsMs(e.ts);
    }
    return null;
  };

  // ================================================================ live stream client
  // fetch() + ReadableStream so the Authorization header works. Reconnects
  // with backoff; after 3 failed attempts falls back to polling /events and
  // keeps trying the stream every 30 s. Each page load has its own id (?c=),
  // so the server ends this page's previous stream when it reconnects.
  const pageId = (() => {
    try { const a = new Uint8Array(12); crypto.getRandomValues(a); return Array.from(a, (x) => x.toString(16).padStart(2, "0")).join(""); }
    catch (e) { return (Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^a-z0-9]/g, "").slice(0, 24); }
  })();
  function liveStream(g, k, on) {
    let cur = null, failures = 0, notFound = 0, retry = null, lastData = 0, polling = null, pollRetry = null, stopped = false;
    let pollMisses = 0;
    const isAlive = () => alive(g) && !stopped;

    async function connect() {
      clearTimeout(retry);
      if (!isAlive()) return;
      const my = new AbortController();
      cur = my;
      let got = false;
      try {
        const r = await fetch(`/api/session/${k}/stream?c=${pageId}`, {
          headers: { Authorization: "Bearer " + token, Accept: "text/event-stream" }, cache: "no-store", signal: my.signal,
        });
        if (r.status === 401) { stop(); showLogin(); return; }
        if (r.status === 404) {
          // One miss can be a slow `tmux list-panes`: only a second one in a row means the session ended.
          if (++notFound >= 2) { stop(); on.gone(); return; }
          throw Object.assign(new Error("HTTP 404"), { retryable: true });
        }
        if (r.status === 503) throw Object.assign(new Error("HTTP 503"), { retryable: true }); // too many streams: not our failure
        if (!r.ok || !r.body) throw new Error("HTTP " + r.status);
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        const sse = new SSEBuffer();
        lastData = Date.now();
        for (;;) {
          const x = await reader.read();
          if (x.done) break;
          if (cur !== my) return;
          lastData = Date.now();
          for (const msg of sse.push(dec.decode(x.value, { stream: true }))) {
            if (cur !== my || !isAlive()) return;
            if (msg.type === "snapshot") { got = true; failures = 0; notFound = 0; stopPolling(); }
            if (msg.type === "gone") { stop(); on.gone(); return; }
            try { on.message(msg); } catch (err) { console.error(err); }
          }
        }
        throw new Error("stream closed");
      } catch (e) {
        if (!isAlive() || cur !== my) return;
        cur = null;
        if (polling) return; // the poll loop retries the stream every 30 s
        if (e.retryable) { retry = setTimeout(connect, e.message === "HTTP 503" ? 5000 : 2000); return; }
        if (!got) failures++;
        if (failures >= 3) { startPolling(); return; }
        retry = setTimeout(connect, backoffDelay(failures));
      }
    }
    function restart() {
      if (!isAlive()) return;
      stopPolling();
      const old = cur; cur = null;
      if (old) old.abort();
      connect();
    }
    async function pollOnce() {
      let ev = null, list = null;
      try { ev = await api(`session/${k}/events?limit=100`); }
      catch (e) {
        if (e.status === 404 && ++pollMisses >= 2 && isAlive() && polling) { stop(); on.gone(); }
        return;
      }
      try { list = await api("sessions"); } catch (e) { return; }
      if (!isAlive() || !polling) return;
      pollMisses = 0;
      on.polled(ev, list.sessions || []);
    }
    function startPolling() {
      if (polling) return;
      on.mode("polling");
      polling = setInterval(() => { if (!document.hidden) pollOnce(); }, 3000);
      pollRetry = setInterval(() => { if (!document.hidden && !cur && isAlive()) connect(); }, 30000);
      pollOnce();
    }
    function stopPolling() {
      if (!polling) return;
      clearInterval(polling); clearInterval(pollRetry); polling = null; pollRetry = null;
      on.mode("stream");
    }
    function stop() {
      stopped = true; clearTimeout(retry); stopPolling();
      const old = cur; cur = null;
      if (old) old.abort();
    }
    // Stall watchdog: the server pings every 5 s.
    const wd = setInterval(() => {
      if (cur && !document.hidden && Date.now() - lastData > 20000) restart();
    }, 5000);
    timers.push(wd);
    const onVis = () => { if (!document.hidden) { failures = 0; restart(); } };
    document.addEventListener("visibilitychange", onVis);
    cleanups.push(() => document.removeEventListener("visibilitychange", onVis), stop);
    ctrl.signal.addEventListener("abort", stop);
    connect();
    return { restart, stop };
  }

  // ================================================================ session view
  function showSession(g, key) {
    const k = encodeURIComponent(key);
    const draftKey = "draft:" + key;
    const S = { session: null, pending: null, agents: [], todos: [], more: false, statusSince: Date.now(),
      lastStatus: null, first: true, ended: false, polling: false, sid: null };
    setHeader("会话", "");

    const topEl = h("button", { type: "button", class: "loading-more link hidden", text: "加载更早的消息", onclick: () => loadOlder(true) });
    const tlEl = h("div", { class: "timeline" });
    const notice = h("div");
    main.replaceChildren(topEl, tlEl, notice);

    const agentHash = (id) => sessHash(key, "agent=" + encodeURIComponent(id));
    const findAgent = (e) => {
      const byId = S.agents.find((a) => a.toolUseId && a.toolUseId === e.id);
      if (byId) return byId;
      const d = (e.input || {}).description;
      if (!d) return null;
      const m = S.agents.filter((a) => a.kind === "task" && a.label === d);
      return m.find((a) => a.status === "running") || m[0] || null;
    };
    const ctx = { k, cwd: () => (S.session && S.session.cwd) || "", findAgent, agentHash, expanded: new Map() };
    const tl = new Timeline(tlEl, ctx);

    // -------- scrolling: stick to bottom, "↓ 新消息" pill, older history
    const pill = $("#newPill");
    const nearBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 160;
    const toBottom = (smooth) => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    let wasNear = true, lastId = null, holdScroll = false;
    tl.hooks.before = () => { wasNear = nearBottom(); };
    tl.hooks.after = () => {
      const last = tl.order[tl.order.length - 1] || null;
      if (holdScroll) { lastId = last; return; }
      if (S.first || wasNear) { toBottom(false); pill.classList.add("hidden"); }
      else if (last !== lastId) pill.classList.remove("hidden");
      lastId = last;
      fill(notice, noticeFor());
      syncTop();
    };
    pill.onclick = () => { toBottom(true); pill.classList.add("hidden"); };
    // Older history: loaded when scrolling near the top, by tapping the
    // button at the top, and on its own while the page is too short to scroll.
    let loadingOlder = false, olderFailed = false;
    function syncTop() {
      const show = S.more && tl.order.length > 0;
      topEl.classList.toggle("hidden", !show);
      topEl.disabled = loadingOlder;
      topEl.textContent = loadingOlder ? "加载中…" : "加载更早的消息";
      if (show && !loadingOlder && !olderFailed && document.documentElement.scrollHeight <= window.innerHeight + 400) {
        setTimeout(() => { if (alive(g)) loadOlder(); }, 0);
      }
    }
    async function loadOlder(manual) {
      if (manual) olderFailed = false;
      if (loadingOlder || !S.more || !tl.order.length || (olderFailed && !manual)) return;
      loadingOlder = true; syncTop();
      try {
        const r = await api(`session/${k}/events?before=${encodeURIComponent(tl.order[0])}&limit=100`);
        if (!alive(g)) return;
        const before = document.documentElement.scrollHeight, first = tl.order[0];
        holdScroll = true;
        tl.prepend(r.events || []);
        holdScroll = false;
        window.scrollBy(0, document.documentElement.scrollHeight - before);
        S.more = !!r.more && tl.order[0] !== first; // no progress: stop instead of asking again
      } catch (e) { olderFailed = true; fail(e); } finally { loadingOlder = false; if (alive(g)) syncTop(); }
    }
    window.onscroll = () => {
      if (nearBottom()) pill.classList.add("hidden");
      if (window.scrollY < 400) loadOlder();
    };

    function noticeFor() {
      const s = S.session;
      if (!s) return null;
      if (s.controllable && !s.sessionId && s.status !== "shell") {
        return h("div", { class: "card small" }, "claude 还没进入对话。如果停在“是否信任此目录”，默认选项是 No：先按 ↓ 再按 ⏎（⌨ 按钮里），或者",
          h("button", { type: "button", class: "link", text: "查看终端", onclick: () => go(sessHash(key, "v=term")) }));
      }
      if (!tl.order.length) return h("div", { class: "empty", text: S.first ? "加载中…" : "还没有对话内容" });
      return null;
    }

    // -------- dock
    const permEl = h("div");
    const spinner = h("span", { class: "spinner" });
    const sdot = h("span", { class: "dot" });
    const stText = h("span", { class: "grow sub" });
    const statusEl = h("div", { class: "statusline" }, spinner, sdot, stText);
    const agentsStrip = h("button", { type: "button", class: "strip hidden", onclick: () => openAgents() });
    const todosStrip = h("button", { type: "button", class: "strip hidden", onclick: () => openTodos() });
    const strips = h("div", { class: "strips" }, agentsStrip, todosStrip);
    const keypop = h("div", { class: "keypop hidden" },
      [["Enter", "⏎"], ["Escape", "Esc"], ["1", "1"], ["2", "2"], ["3", "3"], ["Up", "↑"], ["Down", "↓"], ["BTab", "⇧Tab"], ["C-c", "Ctrl-C"]]
        .map(([kk, label]) => h("button", { type: "button", text: label, onclick: () => sendKey(k, kk) })));
    const modeChip = h("button", { type: "button", class: "mode-chip", onclick: () => openModes() });
    const ta = h("textarea", { rows: "1", placeholder: "给 Claude 发消息…", enterkeyhint: "enter" });
    const kbdBtn = h("button", { type: "button", class: "kbd-btn", text: "⌨", "aria-label": "按键",
      onclick: () => { keypop.classList.toggle("hidden"); } });
    const sendBtn = h("button", { type: "button", class: "primary send-btn", text: "发送" });
    const inputbar = h("div", { class: "inputbar" }, modeChip, ta, kbdBtn, sendBtn);
    const note = h("div", { class: "dock-note hidden" });
    const startRow = h("div", { class: "hidden" },
      h("div", { class: "dock-note", text: "这个窗口里 claude 没在运行。" }),
      h("div", { class: "keys" },
        h("button", { type: "button", class: "primary", text: "启动 claude", onclick: () => start(false) }),
        h("button", { type: "button", text: "接着上次（--continue）", onclick: () => start(true) })));
    setDock(h("div", { class: "dock-in" }, permEl, statusEl, strips, keypop, inputbar, startRow, note));

    ta.value = store.get(draftKey);
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 2, window.innerHeight * 0.36) + "px"; };
    ta.oninput = () => { grow(); store.set(draftKey, ta.value); updateSend(); };
    ta.onkeydown = (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); send(); } };
    requestAnimationFrame(grow);
    const busy = () => S.session && S.session.status === "busy";
    // While a prompt is open, Enter (or a digit) typed into the terminal would
    // answer it, so the composer and the mode switch are off until it is handled.
    const waiting = () => !!S.session && S.session.status === "waiting";
    let sending = false;
    function updateSend() {
      const stop = busy() && !ta.value.trim();
      sendBtn.textContent = stop ? "■ 停止" : "发送";
      sendBtn.classList.toggle("stop", stop);
      sendBtn.classList.toggle("primary", !stop);
      ta.disabled = waiting();
      sendBtn.disabled = waiting() || sending;
      modeChip.disabled = waiting();
      ta.placeholder = waiting() ? "先处理上面的提示…" : busy() ? "排队下一条消息…" : "给 Claude 发消息…";
    }
    sendBtn.onclick = () => send();
    async function send() {
      const text = ta.value;
      if (waiting() || sending) return;
      if (!text.trim()) {
        if (busy()) {
          sending = true; updateSend();
          try { await api(`session/${k}/interrupt`, { method: "POST" }); toast("已发送停止（Esc）", 1200); }
          catch (e) { fail(e); } finally { sending = false; updateSend(); }
        }
        return;
      }
      sending = true; updateSend();
      try {
        await api(`session/${k}/send`, { method: "POST", body: { text } });
        ta.value = ""; store.set(draftKey, ""); grow();
        toBottom(true);
      } catch (e) { fail(e, 4000); } finally { sending = false; updateSend(); }
    }
    async function start(resume) {
      try { await api(`session/${k}/start`, { method: "POST", body: { resume } }); toast("正在启动 claude"); stream.restart(); }
      catch (e) { fail(e, 4000); }
    }

    function openModes() {
      if (waiting()) { toast("先处理完当前的提示再切换模式"); return; }
      const curMode = (S.session && S.session.mode) || "default";
      openSheet("权限模式", MODES.map(([m, label, desc]) => h("button", {
        type: "button", class: "sheet-item" + (m === curMode ? " on" : ""),
        onclick: async () => {
          closeSheet();
          if (m === curMode) return;
          toast("正在切换到“" + label + "”…", 1500);
          try {
            const r = await api(`session/${k}/mode`, { method: "POST", body: { target: m } });
            if (!alive(g)) return;
            if (S.session) S.session.mode = r.mode || m;
            updateDock();
            toast("已切换到“" + (MODE_NAME[r.mode] || label) + "”");
          } catch (e) { fail(e, 4000); }
        },
      }, h("div", { class: "grow" }, h("div", { text: label }), h("div", { class: "sub", text: desc })), m === curMode ? "✓" : null)));
    }

    function agentsList() {
      if (!S.agents.length) return h("div", { class: "empty", text: "没有智能体" });
      return S.agents.slice(0, 30).map((a) => h("button", {
        type: "button", class: "sheet-item", onclick: () => { closeSheet(); go(agentHash(a.id)); },
      }, h("span", { class: "dot " + a.status }),
        h("div", { class: "grow" },
          h("div", { class: "title", text: (a.kind === "workflow" ? "⚙ " : "") + (a.label || a.id) + (a.phase ? " · " + a.phase : "") }),
          a.lastAction ? h("div", { class: "sub", text: a.lastAction }) : null),
        h("span", { class: "small", text: ago(a.updatedAt) })));
    }
    let agentsSheet = false;
    function openAgents() {
      agentsSheet = true;
      openSheet("智能体", agentsList(), () => { agentsSheet = false; });
    }
    function openTodos() {
      openSheet("任务清单", S.todos.map((t) => h("div", { class: "todo " + t.status },
        h("span", { class: "box", text: t.status === "completed" ? "✓" : t.status === "in_progress" ? "▶" : "○" }),
        h("span", { class: "grow", text: t.status === "in_progress" && t.activeForm ? t.activeForm : t.content }))));
    }
    function updateStrips() {
      const running = S.agents.filter((a) => a.status === "running").length;
      agentsStrip.classList.toggle("hidden", !S.agents.length);
      agentsStrip.textContent = running ? `🤖 ${running} 个智能体运行中 ▸` : `🤖 ${S.agents.length} 个智能体（都已完成）▸`;
      const done = S.todos.filter((t) => t.status === "completed").length;
      const curT = S.todos.find((t) => t.status === "in_progress") || S.todos.find((t) => t.status === "pending");
      todosStrip.classList.toggle("hidden", !S.todos.length);
      todosStrip.textContent = `☑ ${done}/${S.todos.length} · ` + (curT ? (curT.status === "in_progress" && curT.activeForm) || curT.content : "全部完成");
      if (agentsSheet && sheetOpen()) { const sh = $("#sheet"); sh.replaceChildren(h("h2", { text: "智能体" })); append(sh, agentsList()); }
    }

    function updateStatusLine() {
      const s = S.session;
      if (!s) { stText.textContent = "连接中…"; sdot.className = "hidden"; return; }
      let text, since = S.statusSince;
      switch (s.status) {
        case "waiting": text = S.pending && S.pending.kind === "question" ? "等待你回答…" : "等待你批准…"; break;
        case "busy": {
          const t = tl.lastRunningTool();
          text = t ? `正在运行 ${toolLabel(t)}…` : "思考中…";
          const u = tl.lastUserTs();
          if (u && u < since && Date.now() - u < 6 * 3600 * 1000) since = u; // the turn started at the prompt
          break;
        }
        case "idle": text = "空闲"; since = null; break;
        case "starting": text = "启动中…"; break;
        case "shell": text = "claude 没在运行"; since = null; break;
        default: text = "状态未知"; since = null;
      }
      if (S.ended) { text = "会话已结束"; since = null; }
      if (S.polling) text += "（实时连接失败，正在轮询）";
      const spin = !S.ended && (s.status === "busy" || s.status === "starting");
      spinner.classList.toggle("hidden", !spin);
      sdot.className = spin ? "hidden" : "dot " + (S.ended ? "unknown" : s.status);
      stText.textContent = text + (since ? " · " + fmtElapsed((Date.now() - since) / 1000) : "");
    }

    function fillNote(s) {
      const resumeBtn = s && s.sessionId && (!s.controllable || S.ended)
        ? h("button", { type: "button", class: "primary", text: "在手机上继续这个对话",
            onclick: () => resumeConversation(s.sessionId, s.title || s.name) }) : null;
      if (S.ended) {
        fill(note, h("div", { text: "会话已结束。" }), resumeBtn);
      } else if (s && !s.controllable) {
        fill(note,
          h("div", { text: (s.source === "claude-vscode" ? "VS Code 会话在手机上只能查看。" : "这个会话不在 tmux 里，只能查看。")
            + "要在手机上回答和发指令：先在电脑上关掉这个会话的面板，再点下面的按钮，会在 tmux 里接着同一个对话。"
            + "以后在 VS Code 里用命令「cswap: 在 tmux 里启动 Claude」开会话，就能一直在手机上操作。" }),
          resumeBtn);
      }
    }
    function updateDock() {
      const s = S.session;
      const ctl = !!(s && s.controllable) && !S.ended;
      const shell = !!s && s.status === "shell";
      inputbar.classList.toggle("hidden", !ctl || shell);
      startRow.classList.toggle("hidden", !ctl || !shell);
      if (!ctl || shell) keypop.classList.add("hidden");
      note.classList.toggle("hidden", !(s && !s.controllable) && !S.ended);
      const noteSig = JSON.stringify([S.ended, s && s.controllable, s && s.source, s && s.sessionId]);
      if (noteSig !== S.noteSig) { S.noteSig = noteSig; fillNote(s); }
      const mode = (s && s.mode) || "default";
      modeChip.textContent = MODE_NAME[mode] || mode;
      modeChip.className = "mode-chip " + mode;
      updateSend();
      updateStatusLine();
      updateStrips();
      renderPending();
      syncDock();
    }

    // -------- permission card
    let permSig = null, answering = false;
    const pdialog = (p) => (p && typeof p.dialog === "string" ? p.dialog : "");
    function renderPending() {
      const p = S.pending, s = S.session;
      if (!p || !s || s.status !== "waiting" || S.ended) { permEl.replaceChildren(); permSig = null; return; }
      const sig = JSON.stringify([s.controllable, p.kind, p.rawHash, p.tool, p.waitingFor, p.question, p.options, p.ambiguous, pdialog(p)]);
      if (sig === permSig) {
        // same prompt: buttons stay off while an answer is in flight
        if (!answering) permEl.querySelectorAll("button, textarea").forEach((b) => { b.disabled = false; });
        return;
      }
      permSig = sig;
      const input = p.input || {};
      const kids = [];
      let title;
      // No tool-specific card when the server cannot tell which call the
      // prompt is about (several open calls, or a sub-agent's): the
      // terminal text is shown instead, expanded.
      // The dialog part of the screen (what the prompt is about) replaces the
      // full terminal text when the server could cut it out.
      const dialog = pdialog(p);
      const showRaw = !!p.raw && !dialog && (p.ambiguous || !p.tool || !(p.options || []).length);
      if (p.kind === "question" || p.tool === "AskUserQuestion") {
        title = "Claude 有问题想问你";
        if (p.question) kids.push(h("div", { class: "perm-q", text: p.question }));
        if (p.multiSelect) kids.push(h("div", { class: "small", text: "多选题：点选项只是勾选或取消勾选，选好后用 ⌨ 里的按键（↓ 和 ⏎）提交。" }));
      } else if (p.ambiguous) {
        title = "Claude 在等你：" + waitText(p.waitingFor || s.waitingFor || "需要确认");
        kids.push(h("div", { class: "small", text: "有多个操作在等待（或是子智能体发起的），请以下面的终端原文为准。" }));
      } else if (p.tool === "Bash") {
        title = "允许执行这条命令？";
        if (input.description) kids.push(h("div", { class: "sub", text: input.description }));
        kids.push(h("pre", { text: input.command || "" }));
      } else if (EDIT_TOOLS.has(p.tool)) {
        const file = input.file_path || input.notebook_path || "";
        title = `允许修改 ${baseName(file) || "文件"}？`;
        kids.push(h("div", { class: "sub", text: rel(file, s.cwd) }));
        const patch = inputPatch(p.tool, input);
        if (patch.length) kids.push(renderPatch(patch, 200));
      } else {
        title = "Claude 在等你：" + waitText(p.waitingFor || s.waitingFor || "需要输入");
        if (p.tool) {
          const sub = toolSubtitle({ name: p.tool, input }, s.cwd);
          kids.push(h("div", { class: "sub", text: p.tool + (sub ? " · " + sub : "") }));
        }
      }
      if (dialog && (!p.tool || p.ambiguous)) kids.push(h("pre", { class: "perm-dialog", text: dialog }));
      if (p.question && p.kind !== "question" && p.tool !== "AskUserQuestion") kids.push(h("div", { class: "perm-q", text: p.question }));
      if (!s.controllable) {
        permEl.replaceChildren(h("div", { class: "perm" }, h("div", { class: "perm-t", text: title }), kids,
          h("div", { class: "small", text: "这个会话只能在电脑上处理。" })));
        return;
      }
      const opts = (p.options || []).map((o, i) => h("button", {
        type: "button", class: i === 0 ? "primary" : "", text: `${o.key}. ${o.label}`, onclick: () => answer(o.key, null, o.label),
      }));
      const alt = h("textarea", { rows: "1", placeholder: "告诉 Claude 换个做法…" });
      const altBtn = h("button", { type: "button", text: "发送" });
      altBtn.onclick = () => { if (alt.value.trim()) answer("esc", alt.value); else answer("esc"); };
      const raw = h("pre", { class: "perm-raw" + (showRaw ? "" : " hidden"), text: p.raw || "" });
      permEl.replaceChildren(h("div", { class: "perm" },
        h("div", { class: "perm-t", text: title }), kids,
        showRaw ? raw : null,
        opts.length ? h("div", { class: "perm-opts" }, opts)
          : h("div", { class: "small", text: "没能识别出选项。请查看终端原文，或用 ⌨ 里的按键操作。" }),
        h("div", { class: "perm-alt" }, alt, altBtn),
        p.raw && !showRaw ? h("button", { type: "button", class: "link", text: "查看终端原文",
          onclick: (ev) => { raw.classList.toggle("hidden"); ev.target.textContent = raw.classList.contains("hidden") ? "查看终端原文" : "收起终端原文"; } }) : null,
        showRaw ? null : raw));
      if (answering) permEl.querySelectorAll("button, textarea").forEach((b) => { if (!b.classList.contains("link")) b.disabled = true; });
    }
    async function answer(choice, text, label) {
      const p = S.pending;
      if (!p || answering) return;
      answering = true;
      permEl.querySelectorAll("button, textarea").forEach((b) => { if (!b.classList.contains("link")) b.disabled = true; });
      const body = { choice, rawHash: p.rawHash };
      if (text) body.text = text;
      if (label != null) body.label = label;
      let ok = false;
      try {
        const r = await api(`session/${k}/answer`, { method: "POST", body });
        ok = true;
        toast(choice === "esc" ? (text ? (r.sentText ? "已拒绝，并把你的意见发给 Claude" : "已拒绝（又出现了新的提示，意见没有发送）") : "已取消")
          : "已选择 " + choice, 1800);
      } catch (e) {
        if (quiet(e)) { answering = false; return; }
        toast(e.message, 3500);
      }
      // Keep the buttons off a little longer: the next status (within about
      // a second) brings the new prompt. If nothing changed, enable them again.
      const sig = permSig;
      setTimeout(() => {
        answering = false;
        if (!alive(g)) return;
        if (permSig === sig) { permSig = null; renderPending(); }
        else permEl.querySelectorAll("button, textarea").forEach((b) => { b.disabled = false; });
      }, ok ? 2500 : 1200);
    }

    // -------- header menu
    setMenu(() => {
      const s = S.session || {};
      openSheet(s.title || s.name || "会话", [
        h("button", { type: "button", class: "sheet-item", text: "终端", onclick: () => { closeSheet(); go(sessHash(key, "v=term")); } }),
        h("button", { type: "button", class: "sheet-item", text: "改动", onclick: () => { closeSheet(); go(sessHash(key, "v=git")); } }),
        s.owned ? h("button", { type: "button", class: "sheet-item danger", text: "结束会话", onclick: async () => {
          closeSheet();
          if (!confirm(`结束 tmux 会话 ${s.tmux}？里面的 claude 会被关闭（对话记录还在，之后可以用“接着上次”继续）。`)) return;
          try { await api(`session/${k}/kill`, { method: "POST" }); store.set(draftKey, ""); go(""); } catch (e) { fail(e); }
        } }) : null,
      ]);
    });

    // -------- applying server messages
    function applyStatus(session, pending) {
      if (!session) return;
      // claude left this pane (back at a shell): keep the conversation, mark the end
      if (S.sid && !session.sessionId && S.session && S.session.sessionId) tl.divider("— claude 已退出 —");
      if (session.status !== S.lastStatus) { S.lastStatus = session.status; S.statusSince = Date.now(); }
      S.session = session;
      S.pending = pending || null;
      setHeader(session.title || session.name || "会话", "",
        (STATUS[session.status] || session.status) + " · " + (session.controllable ? (session.tmux || "tmux") : roTag(session)));
      updateDock();
    }
    function onMessage(msg) {
      switch (msg.type) {
        case "snapshot": {
          S.agents = msg.agents || []; S.todos = msg.todos || [];
          const plan = snapshotPlan(S.sid, msg.session, msg.events);
          applyStatus(msg.session, msg.pending); // first: cards need cwd and agents
          if (plan === "replace") {
            const kept = tl.replaceTail(msg.events || []);
            if (!kept) S.more = !!msg.more;
          } else {
            // An empty snapshot after a conversation (claude exited, or a new
            // one has not written anything yet): keep what is on screen.
            tl.divider(plan === "keep-exit" ? "— claude 已退出 —" : "— 新的对话 —");
          }
          if (msg.session && msg.session.sessionId) S.sid = msg.session.sessionId;
          if (S.first) { S.first = false; tl.render(); toBottom(false); }
          break;
        }
        case "event": case "patch": tl.upsert(msg.event); setTimeout(updateStatusLine, 0); break;
        case "status": applyStatus(msg.session, msg.pending); break;
        case "agents": S.agents = msg.agents || []; updateStrips(); tl.render(); break;
        case "todos": S.todos = msg.todos || []; updateStrips(); break;
        default: break; // ping and unknown types
      }
    }
    let polledMisses = 0;
    function onPolled(ev, list) {
      (ev.events || []).forEach((e) => tl.upsert(e));
      if (Array.isArray(ev.agents)) { S.agents = ev.agents; tl.render(); }
      if (Array.isArray(ev.todos)) S.todos = ev.todos;
      const s = list.find((x) => x.key === key);
      if (!s) { if (++polledMisses >= 2) onGone(); return; }
      polledMisses = 0;
      // /events carries the same `pending` as the stream (older servers: none)
      const pend = s.status !== "waiting" ? null : "pending" in ev ? ev.pending
        : (S.pending && S.pending.rawHash ? S.pending
          : { kind: "dialog", waitingFor: s.waitingFor, tool: null, input: null, question: null, options: [], raw: "", rawHash: "" });
      applyStatus(s, pend);
      if (S.first) { S.first = false; tl.render(); toBottom(false); }
    }
    function onGone() {
      S.ended = true;
      toast("会话已结束");
      updateDock();
    }
    const stream = liveStream(g, k, {
      message: onMessage, polled: onPolled, gone: onGone,
      mode: (m) => { S.polling = m === "polling"; updateStatusLine(); },
    });
    timers.push(setInterval(() => { if (!document.hidden) updateStatusLine(); }, 1000));
    updateDock();
  }

  async function sendKey(k, key) {
    try { await api(`session/${k}/key`, { method: "POST", body: { key } }); toast("已按 " + (KEYNAME[key] || key), 1000); }
    catch (e) { fail(e, 4000); }
  }

  // ================================================================ agent transcript (read-only)
  function showAgent(g, key, agentId) {
    const k = encodeURIComponent(key);
    setHeader("智能体", sessHash(key));
    const tlEl = h("div", { class: "timeline" });
    const msg = h("div", {}, h("div", { class: "empty", text: "加载中…" }));
    main.replaceChildren(tlEl, msg);
    let cwd = "";
    const ctx = { k, cwd: () => cwd, findAgent: () => null, agentHash: () => sessHash(key), expanded: new Map() };
    const tl = new Timeline(tlEl, ctx);
    let first = true, status = null;
    tl.hooks.after = () => { if (first) window.scrollTo(0, document.documentElement.scrollHeight); };
    api("sessions").then((d) => { const s = (d.sessions || []).find((x) => x.key === key); if (s) cwd = s.cwd || ""; }).catch(() => {});
    every(3000, async (force) => {
      if (!force && status && status !== "running") return;
      try {
        const r = await api(`session/${k}/agent/${encodeURIComponent(agentId)}/events`);
        if (!alive(g)) return;
        const a = r.agent || {};
        status = a.status || null;
        setHeader((a.kind === "workflow" ? "⚙ " : "智能体：") + (a.label || agentId), sessHash(key),
          ({ running: "运行中", done: "已完成", failed: "失败或已中断" }[a.status] || a.status || "") + (a.phase ? " · " + a.phase : ""));
        tl.replaceTail(r.events || []);
        fill(msg, tl.order.length ? null : h("div", { class: "empty", text: "还没有内容" }));
        first = false;
      } catch (e) { if (!quiet(e) && alive(g)) msg.replaceChildren(h("div", { class: "empty", text: e.message })); }
    });
  }

  // ================================================================ terminal view
  function showTerm(g, key) {
    const k = encodeURIComponent(key);
    setHeader("终端", sessHash(key));
    const pre = h("pre", { class: "term", text: "加载中…" });
    main.replaceChildren(pre);
    setDock(h("div", { class: "dock-in" }, h("div", { class: "keys" },
      [["Enter", "⏎ 回车"], ["Escape", "Esc"], ["1", "1"], ["2", "2"], ["3", "3"], ["Up", "↑"], ["Down", "↓"], ["BTab", "⇧Tab"], ["C-c", "Ctrl-C"]]
        .map(([kk, label]) => h("button", { type: "button", text: label, onclick: () => { sendKey(k, kk); setTimeout(() => refreshNow && refreshNow(), 350); } })))));
    let last = null;
    every(1500, async () => {
      try {
        const p = await api(`session/${k}/pane`);
        if (!alive(g) || p.text === last) return;
        const stick = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120;
        last = p.text;
        const x = pre.scrollLeft;
        pre.textContent = p.text;
        pre.scrollLeft = x;
        if (stick) window.scrollTo(0, document.documentElement.scrollHeight);
      } catch (e) { if (!quiet(e) && alive(g)) pre.textContent = e.message; }
    });
  }

  // ================================================================ git view
  function showGit(g, key) {
    const k = encodeURIComponent(key);
    setHeader("改动", sessHash(key));
    const load = async () => {
      main.replaceChildren(h("div", { class: "empty", text: "读取中…" }));
      try {
        const r = await api(`session/${k}/git`);
        if (alive(g)) main.replaceChildren(renderGit(r, load));
      } catch (e) { if (!quiet(e) && alive(g)) main.replaceChildren(h("div", { class: "empty", text: e.message })); }
    };
    refreshNow = (force) => { if (force === true) load(); };
    load();
  }
  function renderGit(r, reload) {
    const frag = document.createDocumentFragment();
    if (!r.repo) { frag.appendChild(h("div", { class: "empty", text: (r.cwd || "") + " 不是 git 仓库" })); return frag; }
    frag.appendChild(h("div", { class: "row" }, h("div", { class: "grow small", text: r.top || "" }),
      h("button", { type: "button", text: "刷新", onclick: reload })));
    (r.errors || []).forEach((x) => frag.appendChild(h("div", { class: "small err-text", text: x })));
    if (!r.hasHead) frag.appendChild(h("div", { class: "small", text: "还没有任何提交，下面显示的是已暂存（git add）的内容。" }));
    const parsed = parseUnifiedDiff(r.diff || "");
    const files = (r.files && r.files.length ? r.files : parsed.map((f) => Object.assign({ path: f.path }, patchCountsAR(f.hunks))));
    const cards = new Map();
    if (files.length) {
      const list = h("div", { class: "card files" });
      for (const f of files) {
        list.appendChild(h("button", { type: "button", class: "gfile", onclick: () => {
          const c = cards.get(f.path);
          if (c) c.scrollIntoView({ behavior: "smooth", block: "start" });
        } }, h("span", { class: "grow", text: f.path }),
          h("span", { class: "counts" },
            h("span", { class: "a", text: f.untracked ? "新文件" : f.added == null ? "二进制" : "+" + f.added }),
            h("span", { class: "r", text: f.removed == null ? "" : "−" + f.removed }))));
      }
      frag.appendChild(list);
    } else frag.appendChild(h("div", { class: "empty", text: "没有改动" }));
    if (r.status) { frag.appendChild(h("div", { class: "section", text: "git status" })); frag.appendChild(h("pre", { text: r.status })); }
    for (const f of parsed) {
      const card = h("div", { class: "card" }, h("div", { class: "row" }, h("div", { class: "grow title", text: f.path }), countsBadge(f.hunks)),
        f.meta.length ? h("div", { class: "small", text: f.meta.join("\n") }) : null,
        f.hunks.length ? renderPatch(f.hunks, 1500) : null);
      cards.set(f.path, card);
      frag.appendChild(card);
    }
    if (r.truncated) frag.appendChild(h("div", { class: "small", text: "diff 太大，只显示了前一部分" }));
    if (r.log) { frag.appendChild(h("div", { class: "section", text: "最近提交" })); frag.appendChild(h("pre", { text: r.log })); }
    return frag;
  }
  function patchCountsAR(hunks) { const c = patchCounts(hunks); return { added: c.a, removed: c.r }; }

  // ================================================================ accounts
  function showAccounts(g) {
    setHeader("账号与额度", "");
    const el = h("div", {}, h("div", { class: "empty", text: "加载中…" }));
    main.replaceChildren(el);
    every(30000, async () => {
      try { const d = await api("accounts"); if (alive(g)) renderAccounts(el, d); }
      catch (e) { if (!quiet(e) && alive(g)) el.replaceChildren(h("div", { class: "empty", text: e.message })); }
    });
  }
  function bar(u, max) {
    const v = Math.min(100, u == null ? 0 : u);
    const cls = max != null && u != null && u >= max ? "full" : v >= 80 ? "hi" : "";
    const fill = h("i", { class: cls });
    fill.style.width = v + "%";
    const b = h("div", { class: "bar" }, fill);
    if (max != null && max < 100) { const s = h("s"); s.style.left = max + "%"; b.appendChild(s); }
    return b;
  }
  function renderAccounts(el, d) {
    const list = d.accounts || [];
    if (!list.length) { el.replaceChildren(h("div", { class: "empty", text: "cswap 里还没有账号" })); return; }
    fill(el, list.map((a) => {
      const sw = a.active || a.number == null ? null : h("button", { type: "button", text: "切换到这个" });
      if (sw) {
        sw.onclick = async () => {
          if (!confirm(switchConfirmText(a))) return;
          sw.disabled = true;
          try { await api("accounts/switch", { method: "POST", body: { number: Number(a.number) } }); toast("已切换"); chipAt = 0; route(); }
          catch (e) { fail(e, 4000); sw.disabled = false; }
        };
      }
      const warn = !!switchWarning(a);
      return h("div", { class: "card" },
        h("div", { class: "row" }, h("div", { class: "grow title", text: (a.active ? "▶ " : "") + (a.number == null ? "-" : a.number) + ": " + a.name }),
          h("span", { class: "badge" + (warn ? " warn" : a.active ? " on" : ""), text: accountLabel(a) })),
        a.reason ? h("div", { class: "small" + (warn ? " warn" : ""), text: a.reason }) : null,
        h("div", { class: "grid2" },
          h("div", {}, h("div", { class: "small", text: `5 小时 ${pct(a.usage5h)}% / 上限 ${pct(a.max5h)}%` }), bar(a.usage5h, a.max5h)),
          h("div", {}, h("div", { class: "small", text: `7 天 ${pct(a.usage7d)}% / 上限 ${pct(a.max7d)}%` }), bar(a.usage7d, a.max7d))),
        h("div", { class: "row mt" }, h("div", { class: "grow small",
          text: (a.reserve ? "主账号（其他账号到上限才用） · " : "") + "优先级 " + (a.priority == null ? "-" : a.priority) +
            (a.schedule && a.schedule.length ? " · 时段 " + a.schedule.join(",") : "") }), sw));
    }), h("div", { class: "small", text: "数据来自 cswap 最近一次记录的用量（cswap 自动切换在后台照常运行）。" }));
  }

  route();
})();
