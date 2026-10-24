// A minimal stand-in for the `vscode` module, enough for extension.js and
// lib/ to run under plain Node. require() this before the extension:
//   const stub = require("./vscode-stub");  stub.config.path = "/tmp/x/cswap";
"use strict";

const Module = require("module");

const config = {}; // cswapStatus.* settings the extension reads
const shown = { info: [], warn: [], error: [], docs: [], status: [] };
// Queued answers: input boxes, quick picks (value or fn(items, opts)) and
// warning dialogs (the button to press; undefined = dismiss).
const answers = { input: [], pick: [], warn: [], info: [] };
const executed = []; // [command, ...args]
const contexts = {}; // setContext key -> value
const terminals = [];
const statusItems = [];
const configUpdates = [];
const webviews = {}; // view id -> registered webview view provider
const closeListeners = [];
// What VS Code does when the user closes a terminal (or its program exits).
const closeTerminal = (t) => closeListeners.slice().forEach((fn) => fn(t));

class EventEmitter {
  constructor() {
    this.fns = [];
    this.event = (f) => this.fns.push(f);
  }
  fire(x) {
    this.fns.forEach((f) => f(x));
  }
  dispose() {}
}

class TreeItem {
  constructor(label, state) {
    this.label = label;
    this.collapsibleState = state;
  }
}

const vscode = {
  EventEmitter,
  TreeItem,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  ThemeIcon: class {
    constructor(id, color) {
      this.id = id;
      this.color = color;
    }
  },
  ThemeColor: class {
    constructor(id) {
      this.id = id;
    }
  },
  MarkdownString: class {
    constructor(v) {
      this.value = v || "";
    }
    appendMarkdown(s) {
      this.value += s;
    }
  },
  StatusBarAlignment: { Left: 1 },
  ProgressLocation: { Window: 1, Notification: 2 },
  ConfigurationTarget: { Global: 1 },
  Uri: { parse: (u) => u, file: (f) => f },
  env: { remoteName: undefined, openExternal: () => {} },
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({
      get: (k, d) => config[k] ?? d,
      update: async (k, v, target) => {
        configUpdates.push([k, v, target]);
        config[k] = v;
      },
    }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
    openTextDocument: async (o) => {
      shown.docs.push(o.content);
      return o;
    },
  },
  window: {
    createStatusBarItem: () => {
      const it = {
        show() {
          this.visible = true;
        },
        hide() {
          this.visible = false;
        },
        dispose() {},
      };
      statusItems.push(it);
      return it;
    },
    showInformationMessage: async (m) => (shown.info.push(m), answers.info.shift()),
    showWarningMessage: async (m) => (shown.warn.push(m), answers.warn.shift()),
    showErrorMessage: async (m) => (shown.error.push(m), undefined),
    showInputBox: async () => answers.input.shift(),
    showQuickPick: async (items, opts) => {
      const a = answers.pick.shift();
      return typeof a === "function" ? a(items, opts) : a;
    },
    showTextDocument: async () => {},
    withProgress: (_o, f) => f(),
    setStatusBarMessage: (m) => shown.status.push(m),
    registerTreeDataProvider: () => ({ dispose() {} }),
    registerWebviewViewProvider: (id, provider) => {
      webviews[id] = provider;
      return { dispose() {} };
    },
    onDidCloseTerminal: (fn) => {
      closeListeners.push(fn);
      return { dispose: () => closeListeners.splice(closeListeners.indexOf(fn), 1) };
    },
    createTerminal: (o) => {
      const t = { name: o.name, options: o, sent: [], show() {}, sendText(x) { this.sent.push(x); } };
      terminals.push(t);
      return t;
    },
  },
  commands: {
    registerCommand: () => ({ dispose() {} }),
    executeCommand: (c, ...args) => {
      executed.push([c, ...args]);
      if (c === "setContext") contexts[args[0]] = args[1];
    },
  },
};

const origLoad = Module._load;
Module._load = function (req, ...rest) {
  if (req === "vscode") return vscode;
  return origLoad.call(this, req, ...rest);
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The view VS Code hands a webview view provider. `webview.posted` collects
// postMessage calls; `send(msg)` is a message from the page's script.
function fakeWebviewView() {
  const handlers = [];
  return {
    webview: {
      options: {},
      html: "",
      posted: [],
      postMessage(m) {
        this.posted.push(m);
        return Promise.resolve(true);
      },
      onDidReceiveMessage(fn) {
        handlers.push(fn);
        return { dispose() {} };
      },
    },
    onDidDispose: () => ({ dispose() {} }),
    send: (m) => Promise.all(handlers.map((fn) => fn(m))),
  };
}

module.exports = { vscode, config, shown, answers, executed, contexts, terminals, statusItems, configUpdates, webviews, fakeWebviewView, sleep, closeTerminal };
