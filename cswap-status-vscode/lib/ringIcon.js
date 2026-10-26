// A 16-pixel usage icon for a row in the 账号 list: the 5-hour window as the
// outer ring, the 7-day window as the inner one, coloured like the big rings
// of the web page (yellow within NEAR_MARGIN of the account's own line, red
// at it). Tree rows cannot use theme colours in a file icon, so each icon is
// written twice, for light and dark themes, into a folder of the extension's
// own storage; the file name is a hash of the picture, so a changed picture
// is a new file and VS Code never shows a stale one.

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const f = require("./format");

const KEEP_DAYS = 2; // icons not used for this long are removed
const PRUNE_EVERY_MS = 6 * 3600e3; // a long-lived window prunes this often

const COLORS = {
  light: { track: "#c8c8c8", fine: "#005fb8", near: "#bf8803", over: "#e51400" },
  dark: { track: "#4b4b4b", fine: "#3794ff", near: "#cca700", over: "#f14c4c" },
};

function level(used, max) {
  if (used === null || used === undefined || max === null || max === undefined) return "fine";
  const u = Number(used);
  const m = Number(max);
  return u >= m ? "over" : u >= m - f.NEAR_MARGIN ? "near" : "fine";
}

// One ring: the track, and the used part clockwise from the top. Unknown
// usage leaves only a dashed track.
function ring(r, width, used, max, c) {
  const known = used !== null && used !== undefined;
  const u = known ? Math.max(0, Math.min(100, Number(used))) : 0;
  const dash = known ? "" : ' stroke-dasharray="1.6 1.6"';
  const track = `<circle cx="8" cy="8" r="${r}" fill="none" stroke="${c.track}" stroke-width="${width}"${dash}/>`;
  if (!known || u <= 0) return track;
  const arc =
    u >= 100
      ? `<circle cx="8" cy="8" r="${r}" fill="none" stroke="${c[level(used, max)]}" stroke-width="${width}"/>`
      : `<circle cx="8" cy="8" r="${r}" fill="none" stroke="${c[level(used, max)]}" stroke-width="${width}" pathLength="100" stroke-dasharray="${u.toFixed(1)} 100" transform="rotate(-90 8 8)"/>`;
  return track + arc;
}

// The icon for one account row, as SVG text. dim: an account taken out of
// automatic switching is drawn faded, like its card.
function svg(r, theme, dim) {
  const c = COLORS[theme];
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16"${dim ? ' opacity="0.5"' : ""}>` +
    ring(6.4, 2.4, r.usage5h, r.max5h, c) +
    ring(2.9, 2.2, r.usage7d, r.max7d, c) +
    "</svg>"
  );
}

// Written to a temporary name and renamed, so another window never reads
// (and keeps) half a file.
function writeOnce(dir, text) {
  const file = path.join(dir, `ring-${crypto.createHash("sha1").update(text).digest("hex").slice(0, 16)}.svg`);
  const now = new Date();
  try {
    if (fs.statSync(file).size === Buffer.byteLength(text)) {
      fs.utimesSync(file, now, now); // in use: keep it
      return file;
    }
  } catch {
    // not there yet
  }
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, text);
  fs.renameSync(tmp, file);
  return file;
}

// { light, dark } file paths for a row's icon, or null when there is no
// folder to write to (the caller falls back to a theme icon).
function iconFiles(dir, r, dim) {
  if (!dir) return null;
  try {
    fs.mkdirSync(dir, { recursive: true });
    return { light: writeOnce(dir, svg(r, "light", dim)), dark: writeOnce(dir, svg(r, "dark", dim)) };
  } catch {
    return null;
  }
}

// Removes icons no window has used for KEEP_DAYS (other windows share the
// folder, so recently used ones stay even when this window no longer shows them),
// and temporary files a crash left behind.
function prune(dir, now = Date.now()) {
  if (!dir) return;
  let names = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return; // no folder yet
  }
  for (const name of names) {
    if (!/^ring-[0-9a-f]+\.svg(\.\d+\.tmp)?$/.test(name)) continue;
    const file = path.join(dir, name);
    try {
      const age = now - fs.statSync(file).mtimeMs;
      if (age > (name.endsWith(".tmp") ? 3600e3 : KEEP_DAYS * 86400e3)) fs.unlinkSync(file);
    } catch {
      // another window just removed it
    }
  }
}

// prune() at most every PRUNE_EVERY_MS (call it as often as convenient).
let lastPrune = 0;
function pruneNow(dir, now = Date.now()) {
  if (now - lastPrune < PRUNE_EVERY_MS) return;
  lastPrune = now;
  prune(dir, now);
}

module.exports = { svg, iconFiles, prune, pruneNow, level, COLORS, KEEP_DAYS };
