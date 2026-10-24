#!/usr/bin/env python3
"""Keep Claude Code's reasoning effort the same on every account and model.

Claude Code saves effort per model (settings.json -> modelSettings.<model>.
effortLevel), and each account can be served a different model list or
default model. After a cswap switch a session can therefore land on a model
with no saved effort and fall back to that model's default. This writes one
chosen level for every model this machine knows about.

  pin-effort.py high     choose a level (low, medium, high, xhigh, max) and apply it
  pin-effort.py          apply the chosen level again (what the cswap hook runs)
  pin-effort.py --show   print the chosen level and what settings.json holds
  pin-effort.py --off    stop pinning (settings.json is left as it is)

Only settings.json's modelSettings is written; ~/.claude.json is only read
(for the model names the accounts were offered). No login data is touched.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

LEVELS = ("low", "medium", "high", "xhigh", "max")
# Models to cover even before an account has offered them.
KNOWN_MODELS = ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-5-5")


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def settings_path():
    return claude_dir() / "settings.json"


def state_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cswap-hooks" / "effort.json"


def read_json(p):
    try:
        d = json.loads(Path(p).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(p, data, mode=0o644):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, p)


def model_names(settings):
    names = set(KNOWN_MODELS) | set(settings.get("modelSettings") or {})
    home_json = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    for opt in read_json(home_json).get("additionalModelOptionsCache") or []:
        value = opt.get("value") if isinstance(opt, dict) else None
        if isinstance(value, str) and value.startswith("claude-"):
            names.add(value.split("[", 1)[0])  # "claude-fable-5-1[1m]" -> "claude-fable-5-1"
    return sorted(names)


def apply(level):
    path = settings_path()
    if path.exists() and not read_json(path) and path.read_text("utf-8").strip():
        sys.exit(f"{path} is not valid JSON; not touching it.")
    settings = read_json(path)
    ms = settings.get("modelSettings")
    ms = dict(ms) if isinstance(ms, dict) else {}
    changed = []
    for name in model_names(settings):
        entry = dict(ms.get(name) or {})
        if entry.get("effortLevel") != level:
            entry["effortLevel"] = level
            ms[name] = entry
            changed.append(name)
    if changed:
        settings["modelSettings"] = ms
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        write_json(path, settings, mode)
    return changed


def main(argv):
    state = read_json(state_path())
    if argv and argv[0] == "--show":
        ms = read_json(settings_path()).get("modelSettings") or {}
        print("pinned:", state.get("level") or "(off)")
        for name, entry in sorted(ms.items()):
            print(f"  {name}: {(entry or {}).get('effortLevel', '-')}")
        return 0
    if argv and argv[0] == "--off":
        write_json(state_path(), {"level": None})
        print("effort is no longer pinned.")
        return 0
    if argv:
        level = argv[0].lower()
        if level not in LEVELS:
            print("level must be one of: " + ", ".join(LEVELS), file=sys.stderr)
            return 2
        write_json(state_path(), {"level": level})
    else:
        level = state.get("level")
        if not level:
            return 0  # not pinned: the hook does nothing
    changed = apply(level)
    if argv:
        print(f"effort pinned to {level}" + (f"; updated {', '.join(changed)}" if changed else "; already set")
              + ". New sessions use it; restart sessions that are already open.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
