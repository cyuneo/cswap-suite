# cswap suite

**English** | [한국어](README.ko.md) | [简体中文](README.zh-CN.md)

A set of local tools for people who use several of their own Claude Code accounts. It is built around [claude-swap](https://github.com/cyuneo/claude-swap) (`cswap`), which switches the account Claude Code is logged in to, and adds:

- **Automatic switching.** When the active account gets close to its usage limit, cswap moves to the account with the most room left. Each account can have its own limits and hours, and one account can be kept in reserve, used only when the others are out.
- **A VS Code side bar.** Usage rings for every account, the auto-switch status with pause and resume, and the API worker's tasks and spending.
- **A phone page** (cswap-remote). See your sessions, answer permission prompts and switch accounts from your phone, over your own Tailscale network.
- **An API worker** (a Claude Code plugin). Hand background tasks to a worker billed to your Console API key, or start a cloud session. It has an on/off switch, per-task budgets and a monthly cap.
- **Agent Monitor** (a VS Code extension, in [its own repository](https://github.com/cyuneo/cyuneo-agent-monitor)). Watch all your sessions; when a chat stops at a usage limit, it is resumed after the limit resets or cswap switches accounts.

Everything stays on your machine: settings, logins and logs are never uploaded. There is no remote account pool, and accounts are not shared with anyone.

## Requirements

- Linux. Tested on Ubuntu; the background services use systemd user services.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- VS Code, for the side bar and Agent Monitor (optional).

## Install

Download `cswap-suite.tar.gz` from [Releases](https://github.com/cyuneo/cswap-suite/releases), then:

```bash
tar xzf cswap-suite.tar.gz
./cswap-suite/install.sh          # add --auto to keep auto-switching running as a background service
```

- It installs into `~/.local/share/cswap-suite`, with the commands in `~/.local/bin`. The unpacked folder can be deleted afterwards.
- To update, install a newer bundle the same way, or run `~/.local/share/cswap-suite/install.sh`. Running services restart on the new version.
- Options: `--auto` (auto-switch service), `--remote` (phone page service), `--no-api-worker`, `--no-vscode`, `--no-monitor`, `--force-vscode` (install the bundled extensions even over newer ones). `./install.sh --help` lists them.
- For the services to run while you are logged out, run `loginctl enable-linger` once.

## Adding accounts

- **On this machine:** click ＋ in the cswap side bar in VS Code, or log in to Claude Code in a terminal and run `cswap add`.
- **On another computer that should not log in itself:** use the accounts of a machine that is logged in, through long-lived setup tokens, with usage synced over SSH: `cswap-satellite setup --server <host>`, then `cswap-satellite install-timer`. See [cswap-satellite/README.md](cswap-satellite/README.md) (in Chinese).

## What is in this repository

| Folder | What it is |
| --- | --- |
| [cswap-status-vscode/](cswap-status-vscode/) | The VS Code extension: the cswap side bar and status bar |
| [api-worker/](api-worker/) | The Claude Code plugin for background tasks on your API key or in the cloud |
| [cswap-remote/](cswap-remote/) | The phone page |
| [cswap-satellite/](cswap-satellite/) | Using the accounts from other computers through setup tokens |
| [cswap-hooks/](cswap-hooks/) | What runs after every switch: keeps the reasoning effort the same and writes a log line |
| [suite/](suite/) | The installer, the bundle builder and the tests |

Related repositories:

| Repository | What it is |
| --- | --- |
| [cyuneo/claude-swap](https://github.com/cyuneo/claude-swap) | Our cswap: a fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap) with per-account limits, auto-switch status and pause, and switch hooks |
| [cyuneo/cyuneo-agent-monitor](https://github.com/cyuneo/cyuneo-agent-monitor) | Agent Monitor |

The detailed documentation of each part is in Chinese, in its folder. Notes for maintainers are in [docs/MAINTAINING.md](docs/MAINTAINING.md) and [docs/PUBLISHING.md](docs/PUBLISHING.md).

## License

[PolyForm Noncommercial 1.0.0](LICENSE): you may use, change and share it, but not for commercial purposes. Third-party projects and their licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Acknowledgements

- [claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol (MIT): our cswap is a modified version of it, under its original license.
- Claude Code helped write and review the code.

Claude and Claude Code are products of Anthropic. This is an independent personal project that works with them; it is not affiliated with Anthropic.
