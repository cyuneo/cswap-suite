// Small, escape-first Markdown renderer for transcript text (untrusted).
//
// Every piece of input text goes through esc() before it reaches the output;
// the only markup in the result is markup this file writes itself. Links are
// kept only for http(s) URLs. Raw HTML in the input is shown as text.
//
// Works in the browser (window.CRmd) and in node (module.exports) for tests.
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  // ------------------------------------------------------------ inline
  // Code spans are cut out first (their content is only escaped), then the
  // rest is escaped and the remaining syntax is applied to the escaped text.
  // Placeholders use \u0000, which is stripped from the input beforehand.
  function inline(src) {
    const codes = [];
    let out = "", i = 0;
    const text = String(src);
    while (i < text.length) {
      if (text[i] === "\\" && i + 1 < text.length && /[\\`*_[\]()#+\-.!|~>]/.test(text[i + 1])) {
        codes.push(esc(text[i + 1]));
        out += "\u0000" + (codes.length - 1) + "\u0000";
        i += 2;
        continue;
      }
      if (text[i] === "`") {
        let n = 0;
        while (text[i + n] === "`") n++;
        const fence = "`".repeat(n);
        let j = i + n, close = -1;
        while (j < text.length) {
          const k = text.indexOf(fence, j);
          if (k < 0) break;
          if (text[k + n] !== "`" && text[k - 1] !== "`") { close = k; break; }
          let m = k; while (text[m] === "`") m++;
          j = m;
        }
        if (close >= 0) {
          let body = text.slice(i + n, close).replace(/\n/g, " ");
          if (/^ .*[^ ].* $/.test(body)) body = body.slice(1, -1);
          codes.push("<code>" + esc(body) + "</code>");
          out += "\u0000" + (codes.length - 1) + "\u0000";
          i = close + n;
          continue;
        }
        out += fence;
        i += n;
        continue;
      }
      out += text[i++];
    }
    let h = esc(out);
    // [label](http(s)://url) — anything else keeps only its label. A finished
    // link becomes a placeholder too, so the emphasis rules below never see
    // its markup (target="_blank", an href with * or _); its label gets
    // emphasis on its own.
    h = h.replace(/\[([^\]\n]+)\]\(([^()\s\u0000]+)\)/g, (m, label, url) => {
      if (/^https?:\/\//i.test(url)) {
        codes.push('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + emphasis(label) + "</a>");
        return "\u0000" + (codes.length - 1) + "\u0000";
      }
      return label;
    });
    h = emphasis(h).replace(/\n/g, "<br>");
    // placeholders may nest (a code span inside a link label)
    const restore = (s) => s.replace(/\u0000(\d+)\u0000/g, (m, n) => restore(codes[Number(n)]));
    return restore(h);
  }
  function emphasis(h) {
    return h.replace(/\*\*([^\s*](?:[^*]*?[^\s*])?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__([^\s_](?:[^_]*?[^\s_])?)__(?!\w)/g, "$1<strong>$2</strong>")
      .replace(/(^|[^*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_([^\s_](?:[^_\n]*?[^\s_])?)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~([^\s~](?:[^~\n]*?[^\s~])?)~~/g, "<del>$1</del>");
  }

  // ------------------------------------------------------------ blocks
  const RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
  const RE_HEAD = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RE_HR = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
  const RE_QUOTE = /^ {0,3}> ?/;
  const RE_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const RE_ITEM_EMPTY = /^(\s*)([-*+]|\d{1,9}[.)])\s*$/;
  const RE_TSEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "", inCode = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
      if (c === "`") inCode = !inCode;
      if (c === "|" && !inCode) { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  function isBlockStart(line, next) {
    return RE_FENCE.test(line) || RE_HEAD.test(line) || RE_HR.test(line) || RE_QUOTE.test(line) ||
      RE_ITEM.test(line) || (line.includes("|") && next != null && RE_TSEP.test(next) && next.includes("-"));
  }

  function indentOf(s) {
    const m = /^\s*/.exec(s)[0];
    return m.replace(/\t/g, "    ").length;
  }

  function blocks(lines, depth) {
    const out = [];
    let i = 0;
    if (depth > 12) return "<p>" + inline(lines.join("\n")) + "</p>";
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      let m = RE_FENCE.exec(line);
      if (m) {
        const mark = m[1], lang = m[2] || "";
        const body = [];
        i++;
        while (i < lines.length) {
          const l = lines[i];
          const t = l.trim();
          if (t.length >= mark.length && t[0] === mark[0] && /^(`+|~+)$/.test(t) && t.length >= mark.length) { i++; break; }
          body.push(l);
          i++;
        }
        out.push('<div class="md-code"><div class="md-code-h"><span>' + esc(lang || "代码") +
          '</span><button type="button" class="md-copy" data-copy="1">复制</button></div><pre><code>' +
          esc(body.join("\n")) + "</code></pre></div>");
        continue;
      }

      m = RE_HEAD.exec(line);
      if (m) {
        const n = m[1].length;
        out.push("<h" + n + ">" + inline(m[2]) + "</h" + n + ">");
        i++;
        continue;
      }

      if (RE_HR.test(line)) { out.push("<hr>"); i++; continue; }

      if (RE_QUOTE.test(line)) {
        const body = [];
        while (i < lines.length && lines[i].trim() && RE_QUOTE.test(lines[i])) {
          body.push(lines[i].replace(RE_QUOTE, ""));
          i++;
        }
        out.push("<blockquote>" + blocks(body, depth + 1) + "</blockquote>");
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && RE_TSEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((c) =>
          /^:-+:$/.test(c) ? "c" : /^-+:$/.test(c) ? "r" : "");
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        const cell = (tag, c, k) => "<" + tag + (aligns[k] ? ' class="al-' + aligns[k] + '"' : "") + ">" +
          inline(c || "") + "</" + tag + ">";
        out.push('<div class="md-table"><table><thead><tr>' + head.map((c, k) => cell("th", c, k)).join("") +
          "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + head.map((_, k) => cell("td", r[k], k)).join("") + "</tr>").join("") +
          "</tbody></table></div>");
        continue;
      }

      m = RE_ITEM.exec(line) || RE_ITEM_EMPTY.exec(line);
      if (m) {
        const base = indentOf(m[1]);
        const ordered = /\d/.test(m[2]);
        const start = ordered ? parseInt(m[2], 10) : 1;
        const items = [];
        while (i < lines.length) {
          const l = lines[i];
          const im = RE_ITEM.exec(l) || RE_ITEM_EMPTY.exec(l);
          if (im && indentOf(im[1]) === base && /\d/.test(im[2]) === ordered) {
            const contentIndent = im[1].length + im[2].length + 1;
            const body = [im[3] || ""];
            i++;
            while (i < lines.length) {
              const n = lines[i];
              if (!n.trim()) {
                // blank line: continue the item only if indented content follows
                if (i + 1 < lines.length && lines[i + 1].trim() && indentOf(lines[i + 1]) > base) {
                  body.push(""); i++; continue;
                }
                break;
              }
              const nm = RE_ITEM.exec(n) || RE_ITEM_EMPTY.exec(n);
              if (indentOf(n) > base) {
                body.push(n.slice(Math.min(contentIndent, n.search(/\S/))));
                i++;
                continue;
              }
              if (nm || isBlockStart(n, lines[i + 1])) break;
              body.push(n); // lazy continuation line
              i++;
            }
            items.push(body);
            continue;
          }
          break;
        }
        const tag = ordered ? "ol" : "ul";
        const attr = ordered && start !== 1 ? ' start="' + start + '"' : "";
        out.push("<" + tag + attr + ">" + items.map((b) => {
          const simple = b.length === 1 || b.slice(1).every((x) => !isBlockStart(x) && x.trim());
          return "<li>" + (simple ? inline(b.join("\n")) : blocks(b, depth + 1)) + "</li>";
        }).join("") + "</" + tag + ">");
        continue;
      }

      const para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) {
        para.push(lines[i]);
        i++;
      }
      out.push("<p>" + inline(para.join("\n")) + "</p>");
    }
    return out.join("");
  }

  function render(text) {
    const src = String(text == null ? "" : text).replace(/\u0000/g, "").replace(/\r\n?/g, "\n");
    return blocks(src.split("\n"), 0);
  }

  const api = { render: render, esc: esc, inline: inline };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CRmd = api;
})(typeof window !== "undefined" ? window : this);
