// Which 账号 view shows: the ring cards (a web page, accountsWeb.js) or the
// list (VS Code's own tree, accountsList.js). cswapStatus.accountsStyle:
//   "auto" (default): the cards, and the list in a window where the page did
//     not load (VS Code's web page layer stuck on the client,
//     microsoft/vscode#125993); a new window tries the cards again
//   "rings": always the cards (a page that does not load is reported once)
//   "list": always the list
// package.json shows the views by the context key cswapStatus.accountsList.

const vscode = require("vscode");
const { cfg } = require("./config");

const STYLES = ["auto", "rings", "list"];
let pageFailed = false; // this window: the page did not load
let warned = false;

function style() {
  const s = cfg("accountsStyle", "auto");
  return STYLES.includes(s) ? s : "auto";
}

function showList() {
  const s = style();
  return s === "list" || (s === "auto" && pageFailed);
}

// Sets the context key the two views' `when` read; call at activation and on
// a settings change.
function apply() {
  return vscode.commands.executeCommand("setContext", "cswapStatus.accountsList", showList());
}

// The page did not say "ready" in time.
function onPageFailed() {
  pageFailed = true;
  const s = style();
  if (s === "auto") {
    vscode.window.setStatusBarMessage("$(list-flat) cswap 账号改用列表显示：这个窗口的网页面板没加载出来", 15000);
    return apply();
  }
  if (s !== "rings" || warned) return undefined;
  warned = true;
  const list = "改用列表显示";
  const reload = "重新加载网页面板";
  return Promise.resolve(
    vscode.window.showWarningMessage(
      "cswap 的账号面板没有加载出来（VS Code 的网页面板出了问题，账号数据是好的）。可以改用列表显示，或者重新加载网页面板。",
      list,
      reload
    )
  ).then((pick) => {
    if (pick === list) return vscode.workspace.getConfiguration("cswapStatus").update("accountsStyle", "list", vscode.ConfigurationTarget.Global);
    if (pick === reload) return vscode.commands.executeCommand("workbench.action.webview.reloadWebviewAction");
    return undefined;
  }, () => {});
}

// Tests only: forget this window's failure.
function _reset() {
  pageFailed = false;
  warned = false;
}

module.exports = { style, showList, apply, onPageFailed, _reset, STYLES };
