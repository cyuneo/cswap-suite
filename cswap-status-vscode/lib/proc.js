// Finding and running the helper programs (cswap, api-worker, python, tmux,
// systemctl). Always execFile/spawn with an argv array, never a shell.

const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { cfg } = require("./config");

const localBin = () => path.join(os.homedir(), ".local", "bin");

function isExecutable(f) {
  try {
    fs.accessSync(f, fs.constants.X_OK);
    return fs.statSync(f).isFile();
  } catch (e) {
    return false;
  }
}

function findBin(name, extra) {
  const dirs = [...(extra || []), ...(process.env.PATH || "").split(path.delimiter)];
  for (const d of dirs) {
    if (!d) continue;
    const f = path.join(d, name);
    if (isExecutable(f)) return f;
  }
  return null;
}

// Resolved binaries, keyed by setting + name. Cleared when the settings
// change (resetBins) and re-checked when the cached file disappears.
const bins = new Map();

// Order: the configured path (cswapStatus.<settingKey>), then ~/.local/bin,
// then PATH. Returns an absolute path or null.
function resolveBin(settingKey, name, extraDirs) {
  const key = `${settingKey || ""}:${name}`;
  const cached = bins.get(key);
  if (cached && isExecutable(cached)) return cached;
  let found = null;
  const configured = settingKey ? String(cfg(settingKey, "") || "").trim() : "";
  if (configured) {
    if (path.isAbsolute(configured)) found = isExecutable(configured) ? configured : null;
    else found = findBin(configured, []);
  }
  if (!found) found = findBin(name, [localBin(), ...(extraDirs || [])]);
  if (found) bins.set(key, found);
  else bins.delete(key);
  return found;
}

function resetBins() {
  bins.clear();
}

// Runs bin with args. Never rejects: resolves {code, out, err, timedOut}.
// `input` is written to stdin (stdin is always closed so nothing waits on it).
function exec(bin, args, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    let child;
    try {
      child = cp.execFile(
        bin,
        args,
        { timeout: o.timeout || 30000, env: o.env || process.env, maxBuffer: o.maxBuffer || 8 * 1024 * 1024 },
        (err, stdout, stderr) => {
          const timedOut = !!(err && err.killed && o.timeout !== 0);
          const code = !err ? 0 : typeof err.code === "number" ? err.code : -1;
          const errText = String(stderr || "").trim() || (err ? (timedOut ? "超时" : String(err.message || err)) : "");
          resolve({ code, out: String(stdout || ""), err: errText, timedOut });
        }
      );
    } catch (e) {
      resolve({ code: -1, out: "", err: String(e.message || e), timedOut: false });
      return;
    }
    if (child.stdin) {
      child.stdin.on("error", () => {});
      child.stdin.end(o.input || "");
    }
  });
}

function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

const lastLine = (s) => (s || "").split("\n").filter(Boolean).pop() || "";

module.exports = { localBin, findBin, resolveBin, resetBins, exec, shq, lastLine };
