// cswap Status: its own side bar (and compact status bar entries) showing
// which machine this VS Code window runs on, the claude-swap accounts with
// their 5h/7d usage rings against their limits, what cswap auto is doing,
// and api-worker's spend and tasks.
//
// Runs as a "workspace" extension, so in a Remote-SSH window it executes on
// the remote host and reports THAT machine's cswap. Each tick runs
// `cswap policy list --json` (local store only) and `api-worker list`, keeps
// Dynamic workflows on in ~/.claude/settings.json, and, when
// cswapStatus.satelliteServer is set, syncs usage from that server over SSH.
// No tokens are read or stored here.
//
// The pieces live in lib/; this file wires them up: activation, commands and
// the scheduler tick.

const vscode = require("vscode");
const { runtime, cfg } = require("./lib/config");
const proc = require("./lib/proc");
const format = require("./lib/format");
const accounts = require("./lib/accounts");
const accountsView = require("./lib/accountsView");
const accountsWeb = require("./lib/accountsWeb");
const accountsList = require("./lib/accountsList");
const accountsMode = require("./lib/accountsMode");
const autoswitch = require("./lib/autoswitch");
const satellite = require("./lib/satellite");
const manage = require("./lib/manage");
const workflows = require("./lib/workflows");
const tmux = require("./lib/tmux");
const api = require("./lib/api");
const apiView = require("./lib/apiView");

let timer;
const accountsPage = new accountsWeb.AccountsView();

// One `cswap policy list --json` feeds the 账号 view, the 自动切换 view and
// the account status bar entry.
async function refreshAccounts() {
  await accounts.refresh();
  await autoswitch.update(accounts.state);
  accountsView.render();
  accountsPage.update();
}

const refreshApi = () => apiView.refresh();

const refreshAll = () => Promise.all([refreshAccounts(), refreshApi()]);

const openPanel = () => vscode.commands.executeCommand("workbench.view.extension.cswap");

function tick() {
  workflows.check();
  satellite.maybeSync();
  return refreshAll();
}

function schedule() {
  if (timer) clearInterval(timer);
  const secs = Math.max(15, Number(cfg("refreshSeconds", 60)) || 60);
  tick();
  timer = setInterval(tick, secs * 1000);
}

function activate(context) {
  runtime.extPath = context.extensionPath;
  if (context.logUri) accountsWeb.setLogDir(context.logUri.fsPath);
  if (context.globalStorageUri) accountsList.setIconDir(require("path").join(context.globalStorageUri.fsPath, "ring-icons"));
  accountsMode.apply();
  runtime.refreshAccounts = refreshAccounts;
  runtime.refreshApi = refreshApi;
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 1000);
  item.command = "cswapStatus.openPanel";
  const apiItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 999);
  apiItem.command = "cswapStatus.openPanel";
  accountsView.init(item);
  apiView.init(apiItem);
  const cmd = (id, fn) => vscode.commands.registerCommand(id, fn);
  context.subscriptions.push(
    item,
    apiItem,
    accounts.changed,
    autoswitch.changed,
    api.changed,
    vscode.window.registerWebviewViewProvider("cswapStatus.accounts", accountsPage),
    vscode.window.registerTreeDataProvider("cswapStatus.accountsList", new accountsList.AccountsList()),
    vscode.window.registerTreeDataProvider("cswapStatus.auto", new autoswitch.AutoTree()),
    vscode.window.registerTreeDataProvider("cswapStatus.api", new apiView.ApiTree()),
    cmd("cswapStatus.openPanel", openPanel),
    cmd("cswapStatus.switchAccount", accounts.switchAccount),
    cmd("cswapStatus.dashboard", () => accounts.cswapInTerminal([])),
    cmd("cswapStatus.show", accountsView.show),
    cmd("cswapStatus.refresh", refreshAll),
    cmd("cswapStatus.addAccounts", satellite.addAccounts),
    cmd("cswapStatus.addAccount", manage.addAccount),
    cmd("cswapStatus.accountEnable", (n) => manage.setEnabled(n, true)),
    cmd("cswapStatus.accountDisable", (n) => manage.setEnabled(n, false)),
    cmd("cswapStatus.accountAlias", manage.setAlias),
    cmd("cswapStatus.accountLimits", manage.setLimits),
    cmd("cswapStatus.accountReserveOn", (n) => manage.setReserve(n, true)),
    cmd("cswapStatus.accountReserveOff", (n) => manage.setReserve(n, false)),
    cmd("cswapStatus.accountRemove", manage.remove),
    cmd("cswapStatus.accountRelogin", manage.relogin),
    cmd("cswapStatus.startInTmux", () => tmux.startInTmux(false)),
    cmd("cswapStatus.continueInTmux", () => tmux.startInTmux(true)),
    cmd("cswapStatus.autoPause", autoswitch.pause),
    cmd("cswapStatus.autoResume", autoswitch.resume),
    cmd("cswapStatus.autoThreshold", autoswitch.setThreshold),
    cmd("cswapStatus.showApi", apiView.showApi),
    cmd("cswapStatus.apiRefresh", refreshApi),
    cmd("cswapStatus.apiDispatch", apiView.dispatchTask),
    cmd("cswapStatus.apiTurnOff", () => apiView.setWorkerEnabled(false)),
    cmd("cswapStatus.apiTurnOn", () => apiView.setWorkerEnabled(true)),
    cmd("cswapStatus.apiOpenResult", apiView.openTaskResult),
    cmd("cswapStatus.apiCancel", apiView.cancelTask),
    cmd("cswapStatus.apiClose", apiView.closeTask),
    cmd("cswapStatus.apiOpenSession", apiView.openSession),
    cmd("cswapStatus.apiCollect", apiView.collectTask),
    cmd("cswapStatus.apiKeyAdd", apiView.addKey),
    cmd("cswapStatus.apiKeyBudget", apiView.setKeyBudget),
    cmd("cswapStatus.apiKeyRemove", apiView.removeKey),
    vscode.window.onDidChangeWindowState(autoswitch.onWindowState),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration("cswapStatus")) return;
      if (e.affectsConfiguration("cswapStatus.accountsStyle")) accountsMode.apply();
      // a changed path setting must take effect without a window reload
      proc.resetBins();
      api.reset();
      schedule();
    })
  );
  schedule();
}

function deactivate() {
  if (timer) clearInterval(timer);
  timer = undefined;
}

module.exports = {
  activate,
  deactivate,
  _test: {
    refresh: refreshAccounts,
    refreshApi,
    refreshAll,
    tick,
    proc,
    format,
    accounts,
    accountsView,
    accountsWeb,
    accountsPage,
    autoswitch,
    satellite,
    manage,
    workflows,
    tmux,
    api,
    apiView,
    AutoTree: autoswitch.AutoTree,
    ApiTree: apiView.ApiTree,
    switchAccount: accounts.switchAccount,
    openTaskResult: apiView.openTaskResult,
    dispatchTask: apiView.dispatchTask,
  },
};
