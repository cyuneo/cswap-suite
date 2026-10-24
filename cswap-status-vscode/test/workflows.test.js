// ensureWorkflows (lib/workflows.js) against a temporary CLAUDE_CONFIG_DIR.
//   node test/workflows.test.js
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const stub = require("./vscode-stub");
const { tmpDir } = require("./fakes");

const tmp = tmpDir("workflows");
process.env.CLAUDE_CONFIG_DIR = path.join(tmp, "claude");
stub.config.keepWorkflowsEnabled = true;

const workflows = require("../lib/workflows");
const file = path.join(tmp, "claude", "settings.json");
const read = () => JSON.parse(fs.readFileSync(file, "utf8"));
const write = (s, mode) => fs.writeFileSync(file, typeof s === "string" ? s : JSON.stringify(s), { mode: mode || 0o600 });

assert.strictEqual(workflows.settingsPath(), file);

// no config dir: Claude Code is not set up here, nothing is created
assert.strictEqual(workflows.ensureWorkflows(), false);
assert.ok(!fs.existsSync(path.dirname(file)));

// dir but no settings.json: created with the key
fs.mkdirSync(path.dirname(file));
assert.strictEqual(workflows.ensureWorkflows(), true);
assert.deepStrictEqual(read(), { enableWorkflows: true });

// missing key: added, the other keys and the file mode kept
write({ model: "opus", env: { A: "1" } }, 0o640);
fs.chmodSync(file, 0o640);
assert.strictEqual(workflows.ensureWorkflows(), true);
assert.deepStrictEqual(read(), { model: "opus", env: { A: "1" }, enableWorkflows: true });
assert.strictEqual(fs.statSync(file).mode & 0o777, 0o640);
assert.deepStrictEqual(fs.readdirSync(path.dirname(file)), ["settings.json"]); // no temp file left

// present (true or an explicit false): untouched
assert.strictEqual(workflows.ensureWorkflows(), false);
write({ enableWorkflows: false });
assert.strictEqual(workflows.ensureWorkflows(), false);
assert.deepStrictEqual(read(), { enableWorkflows: false });

// disableWorkflows set by the user: untouched
write({ disableWorkflows: true });
assert.strictEqual(workflows.ensureWorkflows(), false);
assert.deepStrictEqual(read(), { disableWorkflows: true });

// not plain JSON (comments, a broken edit) or not an object: left alone
for (const text of ['{ // comment\n "a": 1 }', "{broken", "[1, 2]"]) {
  write(text);
  assert.strictEqual(workflows.ensureWorkflows(), false);
  assert.strictEqual(fs.readFileSync(file, "utf8"), text);
}

// empty file: treated as {}
write("");
assert.strictEqual(workflows.ensureWorkflows(), true);
assert.deepStrictEqual(read(), { enableWorkflows: true });

// the setting turned off: never writes
stub.config.keepWorkflowsEnabled = false;
write({});
assert.strictEqual(workflows.ensureWorkflows(), false);
assert.deepStrictEqual(read(), {});

// check(): one notice however many times it changes the file
stub.config.keepWorkflowsEnabled = true;
assert.strictEqual(workflows.check(), true);
write({});
assert.strictEqual(workflows.check(), true);
assert.strictEqual(stub.shown.info.filter((m) => m.includes("Dynamic workflows")).length, 1);

console.log("workflows tests: OK");
