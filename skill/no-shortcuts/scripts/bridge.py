#!/usr/bin/env python3
"""Run the no-shortcuts checks as hooks for coding agents other than Claude Code.

Usage:
    python3 bridge.py cursor session-start|shell|stop
    python3 bridge.py codex session-start|pre-tool|stop

Reads the agent's hook JSON on stdin and translates it for scan.py or check_packages.py,
then translates their answer back. All the rules stay in those two scripts.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_FILE = HERE.parent / "SKILL.md"
CLAUDE_PROMPT = "load the no-shortcuts skill"
AGENT_PROMPT = f"read {SKILL_FILE} and follow it"
DEFAULT_CONTEXT = f"Before writing or editing code or UI, {AGENT_PROMPT}."
CORE = {
    "session-start": ["scan.py", "--session-start"],
    "stop": ["scan.py", "--stop"],
    "shell": ["check_packages.py"],
    "pre-tool": ["check_packages.py"],
}
EVENTS = {"cursor": {"session-start", "shell", "stop"},
          "codex": {"session-start", "pre-tool", "stop"}}


def cursor_folder(payload):
    """Cursor's stop event has no cwd, and user hooks run from ~/.cursor, so never use ours."""
    roots = payload.get("workspace_roots") or []
    return (payload.get("cwd") or (roots[0] if roots else None)
            or os.environ.get("CURSOR_PROJECT_DIR"))


def to_core(agent, event, payload):
    """Return the payload for the core script, or None when there is nothing to check."""
    if agent == "codex":
        # Codex says its transcript format isn't stable, so the tests-rerun warning stays off.
        return {**payload, "transcript_path": None}
    folder = cursor_folder(payload)
    if not folder or payload.get("status", "completed") != "completed":
        return None
    core = {"session_id": payload.get("conversation_id") or payload.get("session_id"),
            "cwd": folder}
    if event == "shell":
        core.update(tool_name="Bash", tool_input={"command": payload.get("command", "")})
    return core


def note_text(stdout):
    """Core scripts send user notes as JSON. Unwrap for agents with nowhere to show them."""
    if not stdout.strip():
        return ""
    try:
        return json.loads(stdout).get("systemMessage", "")
    except (ValueError, AttributeError):
        return stdout


def context_text(stdout):
    return (stdout.strip() or DEFAULT_CONTEXT).replace(CLAUDE_PROMPT, AGENT_PROMPT)


def from_core(agent, event, code, stdout, stderr):
    """Return (exit code, stdout, stderr) in the shape the agent's hook documents."""
    if agent == "codex":
        if event == "session-start":
            return 0, context_text(stdout), ""
        if event == "stop" and code == 0:
            return 0, stdout.strip() or json.dumps({"continue": True}), ""
        return code, stdout.strip(), stderr.strip()
    if event == "session-start":
        return 0, json.dumps({"additional_context": context_text(stdout)}), ""
    if event == "stop":
        if code == 2:
            return 0, json.dumps({"followup_message": stderr.strip()}), ""
        return 0, "{}", note_text(stdout)
    if code == 2:
        reason = stderr.strip()
        return 0, json.dumps({"permission": "deny", "user_message": reason,
                              "agent_message": reason}), ""
    # No reply on purpose: "allow" could override the user's own approval settings.
    return 0, "", note_text(stdout)


def run_core(event, core):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    script, *args = CORE[event]
    return subprocess.run([sys.executable, str(HERE / script), *args], input=json.dumps(core),
                          capture_output=True, text=True, encoding="utf-8", env=env)


def bridge(agent, event, stdin_text):
    core = to_core(agent, event, json.loads(stdin_text))
    if core is None:
        return from_core(agent, event, 0, "", "")
    done = run_core(event, core)
    return from_core(agent, event, done.returncode, done.stdout, done.stderr)


def failed(agent, event, error):
    """A broken bridge must never trap a session, and must never grant anything."""
    message = f"no-shortcuts bridge failed and did not check this turn: {error}"
    code, stdout, _ = from_core(agent, event, 0, "", "")
    if agent == "codex" and event == "stop":
        stdout = json.dumps({"systemMessage": message})
    return code, stdout, message


def main(argv):
    agent, event = (argv[1:3] + ["", ""])[:2]
    if event not in EVENTS.get(agent, ()):
        print(__doc__, file=sys.stderr)
        return 1
    try:
        code, stdout, stderr = bridge(agent, event, sys.stdin.read())
    except Exception as error:
        code, stdout, stderr = failed(agent, event, error)
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
