#!/usr/bin/env python3
"""Scan code changed in a session for mechanical shortcuts.

Hook modes read JSON on stdin:  --session-start  --stop
Read-only audit of history:     --audit REPO BASE HEAD
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MAX_BLOCKS = 3
MAX_FILE_BYTES = 1_000_000
MAX_WALK_FILES = 5000
MTIME_MARGIN_SECONDS = 2
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

C_LIKE = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".c", ".cc",
          ".cpp", ".h", ".hpp", ".php", ".swift", ".go", ".rs", ".vue", ".svelte", ".dart"}
SCRIPTING = {".py", ".rb", ".lua", ".luau", ".sh"}
CODE = C_LIKE | SCRIPTING | {".html"}
TYPESCRIPT = {".ts", ".tsx"}
JS_LIKE = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
UI = {".html", ".jsx", ".tsx", ".vue", ".svelte"}

SKIPPED_PATH = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|target|\.next|__pycache__|\.venv|venv)/|\.min\.\w+$")
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")
WAIVER = re.compile(r"no-shortcuts-ok:\s*(\S.*)$")

STUB = re.compile(
    r"raise NotImplementedError|new Error\(\s*[\"'`]not implemented|\bunimplemented!\(|\btodo!\("
    r"|\bTODO\b[:\s-]*implement|in a real (implementation|app|application|system|project)"
    r"|placeholder implementation", re.I)
TEST_DISABLED = re.compile(
    r"\b(it|test|describe)\.(skip|only)\(|\bx(it|test|describe)\(|\bf(it|describe)\("
    r"|@pytest\.mark\.(skip|xfail)\b|\bpytest\.skip\(|@unittest\.(skip|expectedFailure)\b"
    r"|\bt\.Skip\(|#\[ignore\]|\bSkipTest\b|\.skipTest\(")
CHECKER_TARGETED = re.compile(
    r"#\s*type:\s*ignore\[|#\s*noqa:|eslint-disable-(next-)?line\s+[@\w]")
CHECKER = re.compile(
    r"@ts-ignore|@ts-nocheck|eslint-disable|#\s*type:\s*ignore|#\s*noqa\s*$")
JS_CATCH_ONE_LINE = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*(/\*.*?\*/)?\s*\}")
JS_CATCH_OPEN = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*(//.*)?$")
PROMISE_CATCH = re.compile(r"\.catch\(\s*(\(\s*\w*\s*\)|\w+)\s*=>\s*(\{\s*\}|null|undefined)\s*\)")
PY_EXCEPT = re.compile(r"^\s*except(\s+(BaseException|Exception)(\s+as\s+\w+)?)?\s*:\s*(.*)$")
COMMENT = re.compile(r"^\s*(#|//|/\*|\*)")
COMMENT_WORDS = re.compile(r"(#|//|/\*)\s*(.*?)\s*(\*/)?\s*(\}.*)?$")
ASSERTION = re.compile(r"(^\s*assert\s)|\b(assert\w*|expect)\s*\(|\.should\b")
AS_ANY = re.compile(r"\bas any\b")
DEBUG_JS = re.compile(r"\bconsole\.(log|debug)\(|\bdebugger;")
DEBUG_PY = re.compile(r"\bbreakpoint\(\)|\bpdb\.set_trace\(")
UI_PLACEHOLDER = re.compile(r"lorem ipsum|john doe|example\.com", re.I)


def should_block(rule, is_test_file):
    """Decide whether a rule stops Claude from finishing, or only warns.

    This sets how strict the hook feels day to day.
    """
    return rule in {"stub", "test-disabled", "assertion-deleted",
                    "silenced-error", "checker-suppressed"}


@dataclass
class Finding:
    rule: str
    tier: str
    file: str
    line: int
    text: str
    waived: bool = False
    reason: str = ""

    @property
    def fingerprint(self):
        return f"{self.rule}|{self.file}|{self.text.strip()}"

    def describe(self):
        return f"{self.file}:{self.line} [{self.rule}] {self.text.strip()[:100]}"


def git(repo, *args, check=True):
    out = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
                         capture_output=True, text=True, errors="replace")
    if check and out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()}")
    return out


def repo_root(path):
    out = git(path, "rev-parse", "--show-toplevel", check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def head_or_empty(repo):
    out = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return out.stdout.strip() if out.returncode == 0 else EMPTY_TREE


def parse_diff(text):
    """Return ({file: [(line, text)]} added, [(file, line, text)] removed)."""
    added, removed = {}, []
    old_file = new_file = None
    old_no = new_no = 0
    for raw in text.splitlines():
        if raw.startswith("--- "):
            old_file = raw[6:] if raw.startswith("--- a/") else None
        elif raw.startswith("+++ "):
            new_file = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", raw)
            old_no, new_no = int(match[1]), int(match[2])
        elif raw.startswith("+") and new_file:
            added.setdefault(new_file, []).append((new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-") and old_file:
            removed.append((old_file, old_no, raw[1:]))
            old_no += 1
    return added, removed


def collect_changes(repo, base, head):
    target = [head] if head else []
    diff = git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", base, *target).stdout
    added, removed = parse_diff(diff)
    if head:
        return added, removed
    others = git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    for name in others:
        path = Path(repo) / name
        if path.suffix.lower() not in CODE or path.stat().st_size > MAX_FILE_BYTES:
            continue
        lines = path.read_text(errors="replace").splitlines()
        added[name] = list(enumerate(lines, 1))
    return added, removed


def is_scanned(name):
    return Path(name).suffix.lower() in CODE and not SKIPPED_PATH.search(name)


def body_after(lines, index):
    """Return (first code line, comment lines) following lines[index]."""
    comments = []
    for _, text in lines[index + 1:]:
        if not COMMENT.match(text):
            return text, comments
        comments.append(text)
    return None, comments


def explained(comments):
    """A comment of two or more words counts as a stated reason; 'ignore' does not."""
    for text in comments:
        match = COMMENT_WORDS.search(text)
        if match and len(match[2].split()) >= 2:
            return True
    return False


def silence_rule(comments):
    return "silenced-explained" if explained(comments) else "silenced-error"


def silenced_error(lines, index, suffix):
    text = lines[index][1]
    if suffix == ".py":
        match = PY_EXCEPT.match(text)
        if not match:
            return None
        inline = match[4].strip()
        if inline.startswith("pass"):
            return silence_rule([inline])
        following, comments = body_after(lines, index)
        is_empty = (not inline or inline.startswith("#")) and following is not None and following.strip() == "pass"
        return silence_rule(comments + [inline]) if is_empty else None
    if suffix not in C_LIKE:
        return None
    if JS_CATCH_ONE_LINE.search(text):
        return silence_rule([text[text.rindex("catch"):]])
    if PROMISE_CATCH.search(text):
        return "silenced-error"
    if JS_CATCH_OPEN.search(text):
        following, comments = body_after(lines, index)
        if following is not None and following.strip().startswith("}"):
            return silence_rule(comments + [text[text.rindex("catch"):]])
    return None


def rules_for_line(lines, index, suffix, is_test):
    text = lines[index][1]
    if not is_test and STUB.search(text):
        yield "stub"
    if is_test and TEST_DISABLED.search(text):
        yield "test-disabled"
    if CHECKER_TARGETED.search(text):
        yield "suppression-targeted"
    elif CHECKER.search(text):
        yield "checker-suppressed"
    silenced = silenced_error(lines, index, suffix)
    if silenced:
        yield silenced
    if suffix in TYPESCRIPT and AS_ANY.search(text):
        yield "as-any"
    if not is_test and ((suffix in JS_LIKE and DEBUG_JS.search(text))
                        or (suffix == ".py" and DEBUG_PY.search(text))):
        yield "debug-left"
    if suffix in UI and UI_PLACEHOLDER.search(text):
        yield "ui-placeholder"


def valid_waiver(text):
    match = WAIVER.search(text)
    if match and len(match[1].split()) >= 2:
        return match[1].strip()
    return ""


def make_finding(rule, name, line, text, is_test):
    tier = "block" if should_block(rule, is_test) else "warn"
    reason = valid_waiver(text)
    return Finding(rule, tier, name, line, text, bool(reason), reason)


def deleted_assertions(added, removed):
    gone = [(f, n, t) for f, n, t in removed
            if TEST_PATH.search(f) and is_scanned(f) and ASSERTION.search(t)]
    new = {t.strip() for f, lines in added.items() if TEST_PATH.search(f)
           for _, t in lines if ASSERTION.search(t)}
    surplus = len(gone) - len(new)
    if surplus <= 0:
        return []
    unmatched = [item for item in gone if item[2].strip() not in new]
    return [make_finding("assertion-deleted", f, n, t, True) for f, n, t in unmatched[:surplus]]


def scan_added(added):
    found = []
    for name, lines in added.items():
        if not is_scanned(name):
            continue
        suffix = Path(name).suffix.lower()
        is_test = bool(TEST_PATH.search(name))
        for index, (line_no, text) in enumerate(lines):
            for rule in rules_for_line(lines, index, suffix, is_test):
                found.append(make_finding(rule, name, line_no, text, is_test))
    return found


def scan_repo(repo, base="HEAD", head=None):
    if base == "HEAD":
        base = head_or_empty(repo)
    added, removed = collect_changes(repo, base, head)
    return scan_added(added) + deleted_assertions(added, removed)


def code_files(root):
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = [d for d in subfolders
                         if not d.startswith(".") and not SKIPPED_PATH.search(f"{d}/")]
        for name in files:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if is_scanned(relative):
                yield path, relative


def scan_folder(root, since=0):
    """Scan whole files changed since a time. Without git there are no line-level diffs."""
    added = {}
    for path, relative in code_files(root):
        stat = path.stat()
        if stat.st_mtime >= since and stat.st_size <= MAX_FILE_BYTES:
            added[relative] = list(enumerate(path.read_text(errors="replace").splitlines(), 1))
    return scan_added(added)


def folder_is_walkable(root):
    if Path(root).resolve() in (Path.home().resolve(), Path("/")):
        return False
    limit = int(os.environ.get("NO_SHORTCUTS_MAX_FILES", MAX_WALK_FILES))
    return sum(1 for _ in zip(code_files(root), range(limit + 1))) <= limit


# --- Hook plumbing ---

def state_path(session_id):
    root = Path(os.environ.get("NO_SHORTCUTS_STATE_DIR", Path.home() / ".claude/no-shortcuts-state"))
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w-]", "_", session_id or "unknown")
    return root / f"{safe}.json"


def load_state(path):
    return json.loads(path.read_text()) if path.exists() else {"repos": {}}


def prune_old_state(path):
    cutoff = time.time() - 14 * 86400
    for old in path.parent.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink()


def tell_user(message):
    print(json.dumps({"systemMessage": message}))


def session_start(payload):
    print("Before writing or editing code or UI, load the no-shortcuts skill.")
    cwd = payload.get("cwd") or "."
    root = repo_root(cwd)
    path = state_path(payload.get("session_id"))
    state = load_state(path)
    key = root or str(Path(cwd).resolve())
    if key not in state["repos"]:
        if root:
            state["repos"][key] = {"base": head_or_empty(root),
                                   "known": [f.fingerprint for f in scan_repo(root)]}
        elif folder_is_walkable(key):
            state["repos"][key] = {"since": time.time() - MTIME_MARGIN_SECONDS,
                                   "known": [f.fingerprint for f in scan_folder(key)]}
        else:
            print("no-shortcuts: this folder is too large to check, so the scanner is off here.")
            return 0
        path.write_text(json.dumps(state))
    prune_old_state(path)
    return 0


def comparison_base(root, entry):
    base = entry.get("base", "HEAD")
    if base in ("HEAD", EMPTY_TREE):
        return base
    still_ancestor = git(root, "merge-base", "--is-ancestor", base, "HEAD", check=False)
    return base if still_ancestor.returncode == 0 else "HEAD"


def notes_for_user(new):
    notes = [f"Waived {f.describe()} ({f.reason})" for f in new if f.waived]
    notes += [f"Warning {f.describe()}" for f in new if f.tier == "warn" and not f.waived]
    return notes


def stop(payload):
    cwd = payload.get("cwd") or "."
    root = repo_root(cwd)
    path = state_path(payload.get("session_id"))
    state = load_state(path)
    key = root or str(Path(cwd).resolve())
    if root:
        entry = state["repos"].setdefault(key, {"base": "HEAD", "known": []})
        found = scan_repo(root, comparison_base(root, entry))
    elif key in state["repos"]:
        entry = state["repos"][key]
        found = scan_folder(key, entry["since"])
    else:
        return 0
    known = set(entry["known"])
    new = [f for f in found if f.fingerprint not in known]
    blocks = [f for f in new if f.tier == "block" and not f.waived]
    notes = notes_for_user(new)

    if not blocks:
        entry.pop("guard", None)
        path.write_text(json.dumps(state))
        if notes:
            tell_user("no-shortcuts:\n" + "\n".join(notes))
        return 0

    digest = hashlib.sha256("\n".join(sorted(f.fingerprint for f in blocks)).encode()).hexdigest()
    guard = entry.get("guard", {})
    if guard.get("digest") != digest:
        guard = {"digest": digest, "count": 0, "released": False}
    guard["count"] += 1
    if guard["released"] or guard["count"] > MAX_BLOCKS:
        guard["released"] = True
        entry["guard"] = guard
        path.write_text(json.dumps(state))
        lines = [f"Unresolved {f.describe()}" for f in blocks]
        tell_user("no-shortcuts let Claude stop with these unresolved:\n" + "\n".join(lines + notes))
        return 0

    entry["guard"] = guard
    path.write_text(json.dumps(state))
    lines = "\n".join(f.describe() for f in blocks)
    print(f"no-shortcuts found shortcuts in code changed this session:\n{lines}\n"
          "Fix the cause. If a hit is truly correct as written, add "
          "`no-shortcuts-ok: <reason>` on that line and tell the user why.", file=sys.stderr)
    return 2


def audit(repo, base, head):
    for finding in scan_repo(repo, base, head):
        print(f"{finding.tier}\t{finding.describe()}")
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "--audit":
        return audit(*argv[2:5])
    if mode not in ("--session-start", "--stop"):
        print(__doc__, file=sys.stderr)
        return 1
    try:
        payload = json.load(sys.stdin)
        return session_start(payload) if mode == "--session-start" else stop(payload)
    except Exception as error:  # A broken scanner must never trap a session; tell the user instead.
        tell_user(f"no-shortcuts scanner failed and did not check this turn: {error}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
