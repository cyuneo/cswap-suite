#!/usr/bin/env python3
"""A tiny stand-in for Claude Code's screen, used by the tmux tests.

Usage: fake_tui.py <log file> [dialog file]

Draws either the dialog (from the file) or an input box with a mode footer.
Keys behave like Claude Code's where it matters for safety: on the dialog a
digit, Enter or Shift+Tab answers it (Shift+Tab = "allow all edits"), Esc
cancels it; at the input box Shift+Tab cycles the mode, text is typed into
the draft and Enter submits it. Every answer and submitted line is appended
to the log file, so tests can see exactly what reached the "claude".
It never runs anything else.
"""

import os
import sys
import termios
import tty

LOG = sys.argv[1]
DIALOG = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else ""
MODES = ["default", "acceptEdits", "plan"]
FOOTERS = {
    "default": "  ? for shortcuts",
    "acceptEdits": "  ⏵⏵ accept edits on (shift+tab to cycle)",
    "plan": "  ⏸ plan mode on (shift+tab to cycle)",
}
state = {"dialog": bool(DIALOG), "mode": 0, "draft": "", "lines": []}


def log(s):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def draw():
    out = ["\x1b[2J\x1b[H"]
    if state["dialog"]:
        out.append(DIALOG.replace("\n", "\r\n"))
    else:
        rows = state["lines"][-10:] + ["", "─" * 60, "> " + state["draft"], "─" * 60,
                                        FOOTERS[MODES[state["mode"]]]]
        out.append("\r\n".join(rows))
    os.write(1, "".join(out).encode("utf-8"))


def key(data):
    if state["dialog"]:
        if data == "\x1b[Z":
            log("[dialog] shift+tab")
            state["dialog"], state["mode"] = False, MODES.index("acceptEdits")
        elif data == "\x1b":
            log("[dialog] esc")
            state["dialog"] = False
        elif data in ("\r", "\n"):
            log("[dialog] enter")
            state["dialog"] = False
        elif data[:1].isdigit():
            log("[dialog] " + data[:1])
            state["dialog"] = False
        else:
            log("[dialog] other " + repr(data))
        return
    if data == "\x1b[Z":
        state["mode"] = (state["mode"] + 1) % len(MODES)
    elif data == "\x1b":
        pass
    else:
        for ch in data.replace("\x1b[200~", "").replace("\x1b[201~", ""):
            if ch in "\r\n":
                log("[send] " + state["draft"])
                state["lines"].append("> " + state["draft"])
                state["draft"] = ""
            elif ch >= " ":
                state["draft"] += ch


def main():
    fd = 0
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        draw()
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            key(data.decode("utf-8", "replace"))
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
