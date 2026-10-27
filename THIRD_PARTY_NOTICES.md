# Third-Party Notices

Everything in this repository is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE), except the third-party material listed below, which keeps its own license. No third-party code is copied into this repository itself; the release bundle (`cswap-suite.tar.gz`) redistributes the two projects below.

## claude-swap

- **What:** The `cswap` command. The bundle ships a wheel (`cswap/*.whl`) built from our modified fork, which adds per-account limits, auto-switch status, pausing and switch hooks. `cswap-satellite` also follows claude-swap's data layout so it can read the same files.
- **Source:** https://github.com/realiti4/claude-swap (original), https://github.com/cyuneo/claude-swap (our fork, branch `chenyu-account-policy`)
- **Copyright:** Copyright (c) 2026 Onur Cetinkol
- **License:** MIT License. The full text ships as `cswap/LICENSE` in the bundle and inside the wheel (`*.dist-info/licenses/LICENSE`).

## CYUNEO Agent Monitor (and Codicons inside it)

- **What:** The bundle ships the Agent Monitor VS Code extension (`cyuneo-agent-monitor-*.vsix`), built from https://github.com/cyuneo/cyuneo-agent-monitor under the same PolyForm Noncommercial License.
- It contains Microsoft's Codicons icon font (CC BY 4.0) and stylesheet (MIT). Their notices and license texts travel inside that vsix: see its `THIRD_PARTY_NOTICES.md` and `media/codicons/`.

## Installed at install time (not shipped)

`install.sh` downloads these from PyPI on the user's machine; they are not part of this repository or the bundle:

- **claude-swap's dependencies** (textual, rich, platformdirs, truststore and theirs): MIT, BSD-2-Clause, PSF-2.0.
- **Claude Agent SDK for Python** (`claude-agent-sdk`, Anthropic PBC), used by api-worker in a private virtual environment: MIT for the SDK code. Its use, and the Claude Code binary it carries, fall under Anthropic's terms. api-worker runs it with the user's own Console API key.

## Tools used, not shipped

Claude Code, uv, VS Code, tmux, Tailscale, git and systemd are called as separate programs the user installs.

## Trademarks

Claude and Claude Code are products of Anthropic. Their names appear here only to describe what these tools work with. This project is not affiliated with, endorsed by, or sponsored by Anthropic.
