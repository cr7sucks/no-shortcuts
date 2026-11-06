#!/usr/bin/env python3
"""Install or remove the no-shortcuts skill and hooks.

    python3 install.py                        install for Claude Code
    python3 install.py --agents cursor,codex  also add hooks for Cursor and Codex
    python3 install.py --uninstall            remove from all of them
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
    commands = [entry.get("command", "")]
    commands += [item.get("command", "") for item in entry.get("hooks", [])]
    return any(MARKER in command for command in commands)


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
    for event in list(hooks):
        hooks[event] = [entry for entry in hooks[event] if not is_ours(entry)]
        if not hooks[event]:
            del hooks[event]
    return settings


def add_cursor_hooks(settings, command):
    settings.setdefault("version", 1)
    hooks = settings.setdefault("hooks", {})
    for event, step in (("sessionStart", "session-start"), ("beforeShellExecution", "shell"),
                        ("stop", "stop")):
        hooks.setdefault(event, []).append({"command": f"{command} {step}", "timeout": 30})


def add_codex_hooks(settings, command):
    def entry(step):
        return {"hooks": [{"type": "command", "command": f"{command} {step}", "timeout": 30}]}
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("SessionStart", []).append(entry("session-start"))
    hooks.setdefault("PreToolUse", []).append({"matcher": "Bash", **entry("pre-tool")})
    hooks.setdefault("Stop", []).append(entry("stop"))


AGENTS = {
    "cursor": (".cursor/hooks.json", add_cursor_hooks, ""),
    "codex": (".codex/hooks.json", add_codex_hooks,
              "Codex skips new hooks until you trust them: run /hooks in Codex."),
}


def agents_from(argv):
    if "--agents" not in argv:
        return []
    names = argv[argv.index("--agents") + 1].split(",")
    unknown = [name for name in names if name not in AGENTS]
    if unknown:
        sys.exit(f"Unknown agent: {', '.join(unknown)}. Choose from {', '.join(AGENTS)}.")
    return names


def install(claude_dir, agents=(), home=None):
    home = home or claude_dir.parent
    settings_path = claude_dir / "settings.json"
    settings = remove_hooks(load_settings(settings_path))
    # Read every config first, so a broken one stops the install before anything changes.
    others = {name: remove_hooks(load_settings(home / AGENTS[name][0])) for name in agents}
    target = claude_dir / "skills" / "no-shortcuts"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).append({"matcher": "Bash", **hook("check_packages.py")})
    hooks.setdefault("SessionStart", []).append(hook("scan.py", "--session-start"))
    hooks.setdefault("Stop", []).append(hook("scan.py", "--stop"))
    save_settings(settings_path, settings)
    bridge = (target / "scripts" / "bridge.py").as_posix()
    for name, config in others.items():
        path, add_hooks, note = AGENTS[name]
        add_hooks(config, f'python3 "{bridge}" {name}')
        save_settings(home / path, config)
        if note:
            print(note)
    print("Installed. It takes effect in your next session.")


def uninstall(claude_dir, home=None):
    home = home or claude_dir.parent
    settings_path = claude_dir / "settings.json"
    save_settings(settings_path, remove_hooks(load_settings(settings_path)))
    for path, _, _ in AGENTS.values():
        if (home / path).exists():
            save_settings(home / path, remove_hooks(load_settings(home / path)))
    shutil.rmtree(claude_dir / "skills" / "no-shortcuts", ignore_errors=True)
    shutil.rmtree(claude_dir / "no-shortcuts-state", ignore_errors=True)
    print("Removed.")


if __name__ == "__main__":
    home = Path.home() / ".claude"
    uninstall(home) if "--uninstall" in sys.argv else install(home, agents_from(sys.argv))
