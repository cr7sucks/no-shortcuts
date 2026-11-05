#!/usr/bin/env python3
"""Block installs of packages that don't exist on the public registry.

Runs as a PreToolUse hook on Bash commands, reading JSON on stdin.
"""
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NPM, PYPI = "npm", "pypi"
TIMEOUT_SECONDS = 5
INSTALLERS = [
    (("npm", "install"), NPM), (("npm", "i"), NPM), (("npm", "add"), NPM),
    (("pnpm", "add"), NPM), (("pnpm", "install"), NPM), (("pnpm", "i"), NPM),
    (("yarn", "add"), NPM), (("bun", "add"), NPM), (("bun", "install"), NPM),
    (("pip", "install"), PYPI), (("pip3", "install"), PYPI),
    (("python", "-m", "pip", "install"), PYPI), (("python3", "-m", "pip", "install"), PYPI),
    (("uv", "add"), PYPI), (("uv", "pip", "install"), PYPI), (("poetry", "add"), PYPI),
]
CUSTOM_SOURCE_FLAGS = {"--registry", "--index-url", "-i", "--extra-index-url", "--find-links", "-f"}
FLAGS_WITH_VALUE = {"-r", "--requirement", "-c", "--constraint", "-t", "--target",
                    "--prefix", "-e", "--editable", "-w", "--workspace"}
SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
ENV_ASSIGNMENT = re.compile(r"^\w+=")
WAIVER = re.compile(r"#\s*no-shortcuts-ok:\s*(\S.*)$")
LOCAL_ARCHIVE = re.compile(r"\.(whl|tgz|tar\.gz|zip)$")
PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def clean_name(arg, ecosystem):
    """Return the registry name for an install argument, or None if it isn't one."""
    if arg.startswith((".", "/", "~")) or ":" in arg or LOCAL_ARCHIVE.search(arg):
        return None
    if ecosystem == NPM:
        if arg.startswith("@"):
            return "@" + arg[1:].split("@")[0] if "/" in arg else None
        return None if "/" in arg else arg.split("@")[0]
    if "/" in arg:
        return None
    match = PYPI_NAME.match(arg)
    return match[0] if match else None


def split_words(segment):
    try:
        words = shlex.split(segment, comments=True)
    except ValueError:
        return []
    while words and (ENV_ASSIGNMENT.match(words[0]) or words[0] == "sudo"):
        words = words[1:]
    return words


def segment_packages(words):
    for prefix, ecosystem in INSTALLERS:
        if tuple(words[:len(prefix)]) != prefix:
            continue
        args = words[len(prefix):]
        if CUSTOM_SOURCE_FLAGS.intersection(args):
            return []
        found, skip_next = [], False
        for arg in args:
            if skip_next:
                skip_next = False
            elif arg in FLAGS_WITH_VALUE:
                skip_next = True
            elif not arg.startswith("-"):
                name = clean_name(arg, ecosystem)
                if name:
                    found.append((ecosystem, name))
        return found
    return []


def packages_in(command):
    found = []
    for segment in SEGMENT_SPLIT.split(command):
        found += segment_packages(split_words(segment))
    return found


def registry_url(ecosystem, name):
    if ecosystem == NPM:
        return "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")
    return f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"


def lookup_package(ecosystem, name):
    """Return True if it exists, False if the registry says it doesn't, None if unknown."""
    request = urllib.request.Request(registry_url(ecosystem, name), method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as error:
        return False if error.code == 404 else None
    except (OSError, ValueError):
        return None


def uses_private_source(ecosystem, cwd, env):
    if ecosystem == PYPI:
        return bool(env.get("PIP_INDEX_URL"))
    npmrc = Path(cwd or ".") / ".npmrc"
    return npmrc.exists() and "registry=" in npmrc.read_text(errors="replace")


def waiver_reason(command):
    match = WAIVER.search(command)
    return match[1].strip() if match and len(match[1].split()) >= 2 else ""


def decide(payload, lookup, env):
    """Return (exit code, message for Claude, message for the user)."""
    if payload.get("tool_name") != "Bash":
        return 0, "", ""
    command = (payload.get("tool_input") or {}).get("command", "")
    cwd = payload.get("cwd")
    packages = [p for p in packages_in(command) if not uses_private_source(p[0], cwd, env)]
    if not packages:
        return 0, "", ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda package: lookup(*package), packages))
    missing = [name for (_, name), answer in zip(packages, answers) if answer is False]
    unknown = [name for (_, name), answer in zip(packages, answers) if answer is None]
    notes = []
    if unknown:
        notes.append(f"no-shortcuts couldn't confirm these packages exist: {', '.join(unknown)}")
    if missing:
        reason = waiver_reason(command)
        if not reason:
            return 2, (f"Not found on the public registry: {', '.join(missing)}\n"
                       "Check the name before installing, because invented names can be "
                       "registered by attackers. If this is a private package, add "
                       "`# no-shortcuts-ok: <reason>` to the command and tell the user."), ""
        notes.append(f"Waived missing package {', '.join(missing)} ({reason})")
    return 0, "", "\n".join(notes)


def run(stdin_text, lookup, env):
    try:
        code, for_claude, for_user = decide(json.loads(stdin_text), lookup, env)
    except Exception as error:  # A broken check must never stop work; tell the user instead.
        message = f"no-shortcuts package check failed and did not run: {error}"
        return 0, "", json.dumps({"systemMessage": message})
    return code, for_claude, json.dumps({"systemMessage": for_user}) if for_user else ""


def main():
    code, for_claude, for_user = run(sys.stdin.read(), lookup_package, os.environ)
    if for_claude:
        print(for_claude, file=sys.stderr)
    if for_user:
        print(for_user)
    return code


if __name__ == "__main__":
    sys.exit(main())
