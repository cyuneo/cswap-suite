// Fake cswap / api-worker / systemctl programs for the tests. Each one logs
// its argv (and stdin) as one JSON line and answers from a JSON state file
// the test rewrites between steps.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function tmpDir(tag) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `cswap-status-${tag}-`));
}

function script(file, body) {
  fs.writeFileSync(file, `#!/usr/bin/env python3\nimport json, sys, time\n${body}`, { mode: 0o755 });
  return file;
}

// A fake program in dir/name. Its state file maps the first argv word to
// {"out": <json or string>, "err": "...", "code": 0, "sleep": seconds}.
function fakeProgram(dir, name) {
  const state = path.join(dir, `${name}.state.json`);
  const calls = path.join(dir, `${name}.calls.log`);
  fs.writeFileSync(state, "{}");
  const bin = script(
    path.join(dir, name),
    `stdin = sys.stdin.read()
open(${JSON.stringify(calls)}, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
st = json.load(open(${JSON.stringify(state)}))
args = sys.argv[1:]
r = st.get(" ".join(args[:2])) or st.get(args[0] if args else "") or {"err": "unknown command", "code": 2}
if r.get("rejectFlag") and r["rejectFlag"] in args:
    sys.stderr.write("usage: x\\nerror: unrecognized arguments: " + r["rejectFlag"] + "\\n"); sys.exit(2)
if r.get("sleep"): time.sleep(r["sleep"])
out = r.get("out")
if out is not None:
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
    sys.stdout.write("\\n")
if r.get("err"): sys.stderr.write(r["err"])
sys.exit(r.get("code", 0))
`
  );
  return {
    bin,
    set: (s) => fs.writeFileSync(state, JSON.stringify(s)),
    calls: () =>
      fs.existsSync(calls)
        ? fs.readFileSync(calls, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))
        : [],
  };
}

// HOME and PATH pointing into dir, so no real cswap, api-worker or systemctl
// is found; only python3 (for the fakes) is linked in.
function hermetic(dir) {
  const bin = path.join(dir, "bin");
  fs.mkdirSync(bin, { recursive: true });
  const py = (process.env.PATH || "").split(path.delimiter).concat(["/usr/bin", "/usr/local/bin"])
    .map((d) => path.join(d, "python3")).find((f) => fs.existsSync(f));
  if (!py) throw new Error("python3 is needed for the fake programs");
  fs.symlinkSync(py, path.join(bin, "python3"));
  process.env.HOME = dir;
  process.env.PATH = bin;
  return bin;
}

module.exports = { tmpDir, fakeProgram, hermetic };
