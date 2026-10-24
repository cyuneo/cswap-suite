// Tests for static/md.js (the transcript Markdown renderer).
// Run: node tests/md_test.js   (any node >= 14, e.g. the one bundled with VS Code Server)
"use strict";
const assert = require("assert");
const path = require("path");
const md = require(path.join(__dirname, "..", "static", "md.js"));

let failed = 0, passed = 0;
function test(name, fn) {
  try { fn(); passed++; } catch (e) { failed++; console.error("FAIL " + name + "\n  " + e.message); }
}
const r = (s) => md.render(s);
const has = (s, part) => assert.ok(r(s).includes(part), JSON.stringify(s) + " => " + r(s) + "\n  missing " + part);
const lacks = (s, part) => assert.ok(!r(s).includes(part), JSON.stringify(s) + " => " + r(s) + "\n  should not contain " + part);

// Whatever comes out, the only tags present must be ones the renderer writes.
const ALLOWED = new Set(["p", "br", "strong", "em", "del", "code", "pre", "a", "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td", "div", "span", "button"]);
function onlySafeTags(s) {
  const html = r(s);
  const tags = html.match(/<\/?([a-zA-Z0-9]+)/g) || [];
  for (const t of tags) {
    const name = t.replace(/[</]/g, "").toLowerCase();
    assert.ok(ALLOWED.has(name), "unexpected tag <" + name + "> in " + html);
  }
  for (const tag of html.match(/<[^>]*>/g) || []) {
    assert.ok(!/\son\w+\s*=/i.test(tag), "event handler attribute " + tag + " in " + html);
    assert.ok(!/javascript:/i.test(tag), "javascript: inside tag " + tag + " in " + html);
  }
  for (const m of html.matchAll(/href="([^"]*)"/g)) assert.ok(/^https?:\/\//.test(m[1]), "bad href " + m[1]);
  return html;
}

// ---------------------------------------------------------------- XSS
test("img onerror is escaped", () => {
  has('<img src=x onerror="alert(1)">', "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  onlySafeTags('<img src=x onerror="alert(1)">');
});
test("script tag is text", () => { has("<script>alert(1)</script>", "&lt;script&gt;"); onlySafeTags("<script>alert(1)</script>"); });
test("javascript: link drops the link", () => {
  const h = onlySafeTags("[click](javascript:alert(1))");
  assert.ok(!h.includes("<a"), h);
  onlySafeTags("[x](JavaScript:alert`1`)");
  onlySafeTags("[x](data:text/html;base64,PHNjcmlwdD4=)");
  onlySafeTags("[x](vbscript:msgbox)");
});
test("link url cannot break out of the attribute", () => {
  const h = onlySafeTags('[x](https://a.example/"onmouseover="alert(1))');
  assert.ok(!/href="[^"]*"\s*onmouseover/.test(h), h);
  onlySafeTags("[x](https://a.example/`<img src=x onerror=alert(1)>`)");
  onlySafeTags("[<img src=x onerror=alert(1)>](https://a.example/)");
});
test("html inside code fences stays text", () => {
  const h = onlySafeTags("```html\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n```");
  assert.ok(h.includes("&lt;script&gt;alert(1)&lt;/script&gt;"), h);
  assert.ok(h.includes('class="md-code"'), h);
});
test("html in fence language label is escaped", () => {
  onlySafeTags('```"><img/src=x/onerror=alert(1)>\ncode\n```');
});
test("nested backticks", () => {
  has("``a `b` c``", "<code>a `b` c</code>");
  has("`` `<b>` ``", "<code>`&lt;b&gt;`</code>");
  onlySafeTags("`<img src=x onerror=alert(1)>`");
  onlySafeTags("``` `` ` ``` x");
  has("unclosed `code <i>", "unclosed `code &lt;i&gt;");
});
test("html inside inline code and emphasis is escaped", () => {
  has("`<b>x</b>`", "<code>&lt;b&gt;x&lt;/b&gt;</code>");
  onlySafeTags("**<svg onload=alert(1)>**");
  onlySafeTags("> <iframe src=javascript:alert(1)>");
  onlySafeTags("| a | b |\n|---|---|\n| <img src=x onerror=alert(1)> | `<x>` |");
  onlySafeTags("- <a href='javascript:alert(1)'>x</a>\n  - nested <b>");
});
test("NUL bytes cannot forge placeholders", () => {
  onlySafeTags("\u00000\u0000 `<x>` \u00001\u0000");
  has("a\u0000b", "ab");
});
test("quotes are escaped", () => { has(`"'`, "&quot;&#39;"); });

// ---------------------------------------------------------------- syntax
test("headings", () => {
  has("# Title", "<h1>Title</h1>");
  has("### Sub ###", "<h3>Sub</h3>");
  has("###### six", "<h6>six</h6>");
  has("#nospace", "<p>#nospace</p>");
});
test("paragraphs and line breaks", () => {
  assert.strictEqual(r("a\nb\n\nc"), "<p>a<br>b</p><p>c</p>");
});
test("bold italic strike", () => {
  has("**b**", "<strong>b</strong>");
  has("__b__", "<strong>b</strong>");
  has("*i* and _j_", "<em>i</em> and <em>j</em>");
  has("~~s~~", "<del>s</del>");
  lacks("snake_case_name", "<em>");
  lacks("2 * 3 * 4", "<em>");
});
test("inline code", () => { has("use `ls -la` now", "<code>ls -la</code>"); });
test("backslash escapes", () => { lacks("\\*not em\\*", "<em>"); has("\\*x\\*", "*x*"); });
test("links", () => {
  has("[docs](https://example.com/a?b=1&c=2)",
    '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a>');
  has("[x](http://h)", 'href="http://h"');
  has("[rel](./file.md)", "rel");
  lacks("[rel](./file.md)", "<a");
});
test("unordered list", () => {
  assert.strictEqual(r("- a\n- b\n* c"), "<ul><li>a</li><li>b</li><li>c</li></ul>");
  has("- a\n- b", "<ul><li>a</li><li>b</li></ul>");
});
test("ordered list with start", () => {
  has("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>");
  has("3. three\n4. four", '<ol start="3">');
});
test("nested list", () => {
  const h = r("- a\n  - b\n  - c\n- d");
  assert.ok(/<ul><li><p>a<\/p><ul><li>b<\/li><li>c<\/li><\/ul><\/li><li>d<\/li><\/ul>/.test(h), h);
});
test("list item continuation", () => { has("- a\n  more", "<li>a<br>more</li>"); });
test("blockquote", () => {
  has("> quoted\n> more", "<blockquote><p>quoted<br>more</p></blockquote>");
  has("> # h", "<blockquote><h1>h</h1></blockquote>");
});
test("table", () => {
  const h = r("| a | b | c |\n|:--|:-:|--:|\n| 1 | `x|y` | **3** |\n| 4 |");
  assert.ok(h.includes('<div class="md-table"><table><thead><tr><th>a</th><th class="al-c">b</th><th class="al-r">c</th></tr></thead>'), h);
  assert.ok(h.includes("<td class=\"al-c\"><code>x|y</code></td>"), h);
  assert.ok(h.includes('<td class="al-r"><strong>3</strong></td>'), h);
  assert.ok(h.includes("<tr><td>4</td><td class=\"al-c\"></td><td class=\"al-r\"></td></tr>"), h);
});
test("fenced code with language + copy button", () => {
  const h = r("text\n```python\nprint('<hi>')\n\n  indented\n```\nafter");
  assert.ok(h.includes('<div class="md-code"><div class="md-code-h"><span>python</span><button type="button" class="md-copy" data-copy="1">复制</button></div>'), h);
  assert.ok(h.includes("<pre><code>print(&#39;&lt;hi&gt;&#39;)\n\n  indented</code></pre>"), h);
  assert.ok(h.startsWith("<p>text</p>") && h.endsWith("<p>after</p>"), h);
});
test("tilde fence and longer closing fence", () => {
  has("~~~\na\n~~~", "<pre><code>a</code></pre>");
  has("````\n```\ninner\n```\n````", "<pre><code>```\ninner\n```</code></pre>");
});
test("unterminated fence runs to the end", () => { has("```\nabc", "<pre><code>abc</code></pre>"); });
test("horizontal rule", () => { has("a\n\n---\n\nb", "<hr>"); });
test("emphasis inside fences is not applied", () => { has("```\n**x**\n```", "**x**"); });
test("empty / nullish input", () => {
  assert.strictEqual(r(""), "");
  assert.strictEqual(r(null), "");
  assert.strictEqual(r(undefined), "");
});
test("crlf", () => { assert.strictEqual(r("a\r\nb"), "<p>a<br>b</p>"); });
test("esc helper", () => { assert.strictEqual(md.esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;"); });
test("emphasis never reaches into link markup", () => {
  has("[a](http://x) foo_ bar", '<a href="http://x" target="_blank" rel="noopener noreferrer">a</a> foo_ bar');
  has("[a](http://x/*) [b *](http://y)", '<a href="http://x/*" target="_blank"');
  has("[a](https://x/_y_) and _z_", '<a href="https://x/_y_" target="_blank" rel="noopener noreferrer">a</a> and <em>z</em>');
  has("*[a](https://x/) b*", '<em><a href="https://x/" target="_blank" rel="noopener noreferrer">a</a> b</em>');
  has("[**b** `c_d`](https://y)", '<a href="https://y" target="_blank" rel="noopener noreferrer"><strong>b</strong> <code>c_d</code></a>');
  onlySafeTags("[a](http://x) foo_ bar _[b](https://y/_\"onx=1_)_");
});
test("large input is fast", () => {
  const big = ("**a** `b` [c](https://d) _e_ \n").repeat(5000) + "*".repeat(20000) + "`".repeat(5000);
  const t = Date.now(); r(big); assert.ok(Date.now() - t < 2000, "too slow: " + (Date.now() - t) + "ms");
});

console.log(passed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
