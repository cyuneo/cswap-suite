// Tests for the DOM-free helpers in static/app.js (SSE framing, diff parsing,
// tool-call folding). Run: node tests/app_test.js
"use strict";
const assert = require("assert");
const path = require("path");
const app = require(path.join(__dirname, "..", "static", "app.js"));

let failed = 0, passed = 0;
function test(name, fn) {
  try { fn(); passed++; } catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.stack); }
}

test("SSE: one frame per chunk", () => {
  const b = new app.SSEBuffer();
  assert.deepStrictEqual(b.push('data: {"type":"ping"}\n\n'), [{ type: "ping" }]);
});
test("SSE: several frames in one chunk", () => {
  const b = new app.SSEBuffer();
  const out = b.push('data: {"type":"a"}\n\ndata: {"type":"b"}\n\n: comment\n\ndata: {"type":"c"}\n\n');
  assert.deepStrictEqual(out.map((m) => m.type), ["a", "b", "c"]);
});
test("SSE: frame split across chunks, including inside the separator and a multibyte char", () => {
  const b = new app.SSEBuffer();
  const enc = new TextEncoder().encode('data: {"type":"event","event":{"text":"你好"}}\r\n\r\ndata: {"type":"x"}\n\n');
  const dec = new TextDecoder();
  const out = [];
  for (let i = 0; i < enc.length; i += 3) out.push(...b.push(dec.decode(enc.slice(i, i + 3), { stream: true })));
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].event.text, "你好");
  assert.strictEqual(out[1].type, "x");
  assert.strictEqual(b.buf, "");
});
test("SSE: incomplete frame waits", () => {
  const b = new app.SSEBuffer();
  assert.deepStrictEqual(b.push('data: {"type":"a"}\n'), []);
  assert.deepStrictEqual(b.push("\n"), [{ type: "a" }]);
});
test("SSE: bad json and non-objects are ignored", () => {
  const b = new app.SSEBuffer();
  assert.deepStrictEqual(b.push("data: {broken\n\ndata: 3\n\nevent: x\n\n"), []);
});
test("SSE: multi-line data joins with newline", () => {
  assert.deepStrictEqual(app.parseFrame('data: {"a":\ndata: 1}'), { a: 1 });
});

test("diff parsing", () => {
  const text = [
    "diff --git a/x.py b/x.py", "index 1..2 100644", "--- a/x.py", "+++ b/x.py",
    "@@ -1,2 +1,3 @@", " a", "-b", "+c", "+d",
    "@@ -10 +11 @@", "-z", "+y",
    "diff --git a/img.png b/img.png", "Binary files a/img.png and b/img.png differ",
  ].join("\n");
  const files = app.parseUnifiedDiff(text);
  assert.strictEqual(files.length, 2);
  assert.strictEqual(files[0].path, "x.py");
  assert.strictEqual(files[0].hunks.length, 2);
  assert.deepStrictEqual(files[0].hunks[0], { oldStart: 1, oldLines: 2, newStart: 1, newLines: 3, lines: [" a", "-b", "+c", "+d"] });
  assert.strictEqual(files[0].hunks[1].oldLines, 1);
  assert.deepStrictEqual(app.patchCounts(files[0].hunks), { a: 3, r: 2 });
  assert.deepStrictEqual(files[1].meta, ["Binary files a/img.png and b/img.png differ"]);
});
test("input patch for Edit / MultiEdit / Write", () => {
  assert.deepStrictEqual(app.patchCounts(app.inputPatch("Edit", { old_string: "a\nb", new_string: "c" })), { a: 1, r: 2 });
  assert.deepStrictEqual(app.patchCounts(app.inputPatch("MultiEdit", { edits: [{ old_string: "a", new_string: "b" }, { old_string: "", new_string: "x\ny" }] })), { a: 3, r: 1 });
  assert.deepStrictEqual(app.patchCounts(app.inputPatch("Write", { content: "1\n2\n3" })), { a: 3, r: 0 });
  assert.deepStrictEqual(app.inputPatch("Bash", { command: "ls" }), []);
});

const tool = (id, name, status) => ({ id, kind: "tool", name, status: status || "ok", input: {} });
test("fold: 3+ plain tool calls between texts fold, thinking rides along", () => {
  const segs = app.segment([
    { id: "t0", kind: "text", text: "x" },
    tool("a", "Read"), { id: "th", kind: "thinking" }, tool("b", "Bash"), tool("c", "Grep"),
    { id: "t1", kind: "text", text: "y" },
  ]);
  assert.deepStrictEqual(segs.map((s) => s.kind), ["one", "fold", "one"]);
  assert.strictEqual(segs[1].tools.length, 3);
  assert.strictEqual(segs[1].evs.length, 4);
});
test("fold: edits, errors, agents and running tools break runs and stay visible", () => {
  const segs = app.segment([
    tool("a", "Read"), tool("b", "Read"), tool("e", "Edit"), tool("c", "Read"), tool("d", "Bash", "error"),
    tool("f", "Read"), tool("g", "Read"), tool("h", "Read"), tool("r", "Bash", "running"),
  ]);
  assert.deepStrictEqual(segs.map((s) => s.kind), ["one", "one", "one", "one", "one", "fold", "one"]);
  assert.strictEqual(segs[5].evs[0].id, "f");
});
test("thinking runs collapse", () => {
  const segs = app.segment([{ id: 1, kind: "thinking" }, { id: 2, kind: "thinking" }, { id: 3, kind: "text", text: "a" }, { id: 4, kind: "thinking" }]);
  assert.deepStrictEqual(segs.map((s) => s.kind + (s.evs ? s.evs.length : "")), ["think2", "one", "think1"]);
});
test("misc formatting", () => {
  assert.strictEqual(app.fmtElapsed(12), "12 秒");
  assert.strictEqual(app.fmtElapsed(125), "2 分 05 秒");
  assert.strictEqual(app.ago(1000, 13000), "12 秒前");
  assert.strictEqual(app.ago(0), "");
  assert.deepStrictEqual(app.clipLines("1\n2\n3", 2), { text: "1\n2", more: 1 });
});

test("reconnect backoff is 1 s, 2 s, 5 s, then 10 s", () => {
  assert.deepStrictEqual([0, 1, 2, 3, 4, 9].map(app.backoffDelay), [1000, 1000, 2000, 5000, 10000, 10000]);
});

test("unchanged events are recognised, so their cards are reused", () => {
  const a = { id: "t1", kind: "tool", status: "ok", result: { text: "x" } };
  assert.ok(app.sameEvent(a, JSON.parse(JSON.stringify(a))));
  assert.ok(!app.sameEvent(a, Object.assign({}, a, { status: "error" })));
  assert.ok(!app.sameEvent(a, null));
});
test("an empty snapshot after a conversation keeps the timeline", () => {
  const ev = [{ id: "u1", kind: "user" }];
  assert.strictEqual(app.snapshotPlan(null, { sessionId: "A" }, ev), "replace");
  assert.strictEqual(app.snapshotPlan(null, { sessionId: null }, []), "replace");
  assert.strictEqual(app.snapshotPlan("A", { sessionId: "A" }, ev), "replace");
  assert.strictEqual(app.snapshotPlan("A", { sessionId: null }, []), "keep-exit");
  assert.strictEqual(app.snapshotPlan("A", { sessionId: "B" }, []), "keep-new");
  assert.strictEqual(app.snapshotPlan("A", { sessionId: "B" }, ev), "replace");
});

test("account state: the machine field wins, the status text is the fallback", () => {
  assert.strictEqual(app.accountState({ state: "limit", status: "Ready" }), "limit");
  assert.strictEqual(app.accountState({ status: "Policy-limit (5h)", enabled: true }), "limit");
  assert.strictEqual(app.accountState({ status: "Disabled", enabled: false }), "disabled");
  assert.strictEqual(app.accountState({ status: "Off-schedule" }), "off-schedule");
  assert.strictEqual(app.accountState({ status: "Reserve", active: true }), "reserve");
  assert.strictEqual(app.accountState({ status: "Active", active: true }), "active");
  assert.strictEqual(app.accountState({ status: "Ready" }), "ready");
  assert.strictEqual(app.accountLabel({ state: "limit", blockedWindow: "7d" }), "到上限（7d）");
  assert.strictEqual(app.accountLabel({ status: "Policy-limit (5h)" }), "到上限（5h）");
  assert.strictEqual(app.accountLabel({ state: "reserve", active: true }), "主账号");
});
test("switching to a disabled, over-limit or off-schedule account asks first", () => {
  assert.strictEqual(app.switchWarning({ number: "3", state: "ready" }), null);
  assert.strictEqual(app.switchWarning({ number: "2", status: "Disabled", enabled: false }), "账号 2 已停用");
  assert.strictEqual(app.switchWarning({ number: "3", state: "limit", blockedWindow: "5h" }), "账号 3 已到 5h 上限");
  assert.strictEqual(app.switchWarning({ number: "4", status: "Off-schedule" }), "账号 4 不在它的使用时段");
  const t = app.switchConfirmText({ number: "2", state: "disabled", reason: "personal 5h limit: 32% / 5%" });
  assert.ok(t.includes("自动切换会在约 1 分钟内切回"), t);
  assert.ok(t.includes("personal 5h limit: 32% / 5%"), t);
  assert.ok(!app.switchConfirmText({ number: "1", state: "ready" }).includes("自动切换"));
});

console.log(passed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
