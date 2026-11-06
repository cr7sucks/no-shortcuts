#!/usr/bin/env python3
"""Install or remove the no-shortcuts skill and hooks for Claude Code.

    python3 install.py              install
    python3 install.py --uninstall  remove
"""
import json
import shutil
import sys
from pathlib import Path

MARKER = "no-shortcuts/scripts/"
SOURCE = Path(__file__).resolve().parent / "skill" / "no-shortcuts"


def hook(script, mode=""):
    command = f'python3 "$HOME/.claude/skills/{MARKER}{script}" {mode}'.strip()
    return {"hooks": [{"type": "command", "command": command, "timeout": 30}]}


def is_ours(entry):
    return any(MARKER in item.get("command", "") for item in entry.get("hooks", []))


def load_settings(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        sys.exit(f"{path} is not valid JSON ({error}). Nothing was changed.")


def save_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-no-shortcuts"))
    path.write_text(json.dumps(settings, indent=2) + "\n")


def remove_hooks(settings):
    hooks = settings.get("hooks", {})
    for event in ("PreToolUse", "SessionStart", "Stop"):
        if event in hooks:
            hooks[event] = [entry for entry in hooks[event] if not is_ours(entry)]
            if not hooks[event]:
                del hooks[event]
    return settings


def install(claude_dir):
    settings_path = claude_dir / "settings.json"
    settings = remove_hooks(load_settings(settings_path))
    target = claude_dir / "skills" / "no-shortcuts"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append({"matcher": "Bash", **hook("check_packages.py")})
    hooks.setdefault("SessionStart", []).append(hook("scan.py", "--session-start"))
    hooks.setdefault("Stop", []).append(hook("scan.py", "--stop"))
    save_settings(settings_path, settings)
    print("Installed. It takes effect in your next Claude Code session.")


def uninstall(claude_dir):
    settings_path = claude_dir / "settings.json"
    save_settings(settings_path, remove_hooks(load_settings(settings_path)))
    shutil.rmtree(claude_dir / "skills" / "no-shortcuts", ignore_errors=True)
    shutil.rmtree(claude_dir / "no-shortcuts-state", ignore_errors=True)
    print("Removed.")


if __name__ == "__main__":
    home = Path.home() / ".claude"
    uninstall(home) if "--uninstall" in sys.argv else install(home)
