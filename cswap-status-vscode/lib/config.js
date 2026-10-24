// Settings access and the few hooks the modules share. Modules never require
// each other in a circle: anything that has to "refresh everything" calls the
// functions extension.js puts in `runtime` at activation.

const vscode = require("vscode");

const runtime = {
  extPath: "",
  refreshAccounts: async () => {},
  refreshApi: async () => {},
  ownSwitchAt: 0, // Date.now() of the last switch started from this window
};

function cfg(key, def) {
  const v = vscode.workspace.getConfiguration("cswapStatus").get(key, def);
  return v === undefined || v === null ? def : v;
}

// "compact" (default): a few characters per entry, details in the cswap side
// bar; "full": the long text of 0.5.0 and earlier; "off": no status bar entries.
function barMode() {
  const m = cfg("statusBar", "compact");
  return m === "full" || m === "off" ? m : "compact";
}

function showItem(it) {
  if (barMode() === "off") it.hide();
  else it.show();
}

module.exports = { runtime, cfg, barMode, showItem };
