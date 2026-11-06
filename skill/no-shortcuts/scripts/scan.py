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
from collections import Counter
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
    r"(^|/)(tests?|__tests__|spec|specs|e2e)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$"
    r"|_spec\.(rb|lua|luau)$|[A-Z]\w*Tests?\.(java|kt|swift|cs)$")
WAIVER = re.compile(r"no-shortcuts-ok:\s*(\S.*)$")
DELETION_WAIVER = re.compile(r"no-shortcuts-deleted-ok:\s*(\S.*)$")

STUB = re.compile(
    r"raise NotImplementedError|new Error\(\s*[\"'`]not implemented|\bunimplemented!\(|\btodo!\("
    r"|\bTODO\b[:\s-]*implement|in a real (implementation|app|application|system|project)"
    r"|placeholder implementation|\b(error|panic|fatalError)\(\s*[\"'`]not implemented"
    r"|UnsupportedOperationException\(\s*\"not implemented|\bTODO\(\)", re.I)
TEST_DISABLED = re.compile(
    r"\b(it|test|describe)\.(skip|only)\(|\bx(it|test|describe)\(|\bf(it|describe)\("
    r"|@pytest\.mark\.(skip|xfail)\b|\bpytest\.skip\(|@unittest\.(skip|expectedFailure)\b"
    r"|\bt\.Skip\(|#\[ignore\]|\bSkipTest\b|\.skipTest\(|@Disabled\b|@Ignore\b|\bXCTSkip\b")
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
STRICT_ASSERTION = re.compile(
    r"assert(Equal|NotEqual|In|Raises|AlmostEqual|Greater|Less|IsNone)\w*\("
    r"|\.(toBe|toEqual|toStrictEqual|toThrow|toContain|toHaveLength)\(|^\s*assert\s.*(==|!=|<|>|\bin\b)")
LOOSE_ASSERTION = re.compile(
    r"assertTrue\(|assertIsNotNone\(|\.toBeTruthy\(|\.toBeDefined\(|\.not\.toBeNull\("
    r"|^\s*assert\s+[\w.()]+\s*(#.*)?$")
TAUTOLOGY = re.compile(
    r"^\s*assert\s+True\b|assertTrue\(\s*True\s*\)|expect\(\s*true\s*\)\.toBe(Truthy\(|\(\s*true\s*\))"
    r"|^\s*assert\s+(?P<a>\d+)\s*==\s*(?P=a)\b|assertEqual\(\s*(?P<b>\d+)\s*,\s*(?P=b)\s*\)")
CI_PATH = re.compile(
    r"(^|/)\.github/workflows/[^/]+\.ya?ml$|(^|/)\.circleci/[^/]+\.ya?ml$|(^|/)\.gitlab-ci\.ya?ml$"
    r"|(^|/)(azure|bitbucket)-pipelines\.ya?ml$|(^|/)Jenkinsfile$|(^|/)ci[\w.-]*\.sh$")
TEST_COMMAND = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|rspec|go test|cargo test|(npm|pnpm|yarn)( run)? test"
    r"|make test|gradle test|mvn test)\b|\bci\.sh\b")
CI_BYPASS = re.compile(r"\|\|\s*(true\b|:\s*$|exit 0\b)|;\s*exit 0\b")
CI_SOFT_FAIL = re.compile(
    r"continue-on-error:\s*true|allow_failure:\s*true|^\s*exit 0\s*$|\bset \+e\b"
    r"|--passWithNoTests|--exit-zero\b")
TEST_WORDS = re.compile(r"\btests?\b|\bspec\b|\btest_\w+|\w+\.test\.")
NOT_A_TEST_RUN = re.compile(r"^\s*(git|echo|cat|grep|rg|ls|sed|find|head|tail)\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GO_IGNORED_ERROR = re.compile(r"^\s*_\s*=\s*err\b")
RUBY_RESCUE_NIL = re.compile(r"\brescue\s+nil\b")
PCALL_IGNORED = re.compile(r"^\s*pcall\(")
RUST_UNWRAP = re.compile(r"\.unwrap\(\)")
AS_ANY = re.compile(r"\bas any\b")
DEBUG_JS = re.compile(r"\bconsole\.(log|debug)\(|\bdebugger;")
DEBUG_PY = re.compile(r"\bbreakpoint\(\)|\bpdb\.set_trace\(")
UI_PLACEHOLDER = re.compile(r"lorem ipsum|john doe|example\.com", re.I)


def should_block(rule, is_test_file):
    """Decide whether a rule stops Claude from finishing, or only warns.

    This sets how strict the hook feels day to day.
    """
    return rule in {"stub", "test-disabled", "assertion-deleted", "test-tautology",
                    "ci-bypass", "ci-step-removed", "silenced-error", "checker-suppressed"}


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
        wanted = path.suffix.lower() in CODE or CI_PATH.search(name)
        if not wanted or path.stat().st_size > MAX_FILE_BYTES:
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
    if is_test and TAUTOLOGY.search(text):
        yield "test-tautology"
    if CHECKER_TARGETED.search(text):
        yield "suppression-targeted"
    elif CHECKER.search(text):
        yield "checker-suppressed"
    silenced = silenced_error(lines, index, suffix)
    if silenced:
        yield silenced
    if (suffix == ".go" and GO_IGNORED_ERROR.search(text)) or (suffix == ".rb" and RUBY_RESCUE_NIL.search(text)):
        yield "silenced-error"
    if suffix in {".lua", ".luau"} and PCALL_IGNORED.search(text):
        yield "pcall-ignored"
    if suffix == ".rs" and not is_test and RUST_UNWRAP.search(text):
        yield "rust-unwrap"
    if suffix in TYPESCRIPT and AS_ANY.search(text):
        yield "as-any"
    if not is_test and ((suffix in JS_LIKE and DEBUG_JS.search(text))
                        or (suffix == ".py" and DEBUG_PY.search(text))):
        yield "debug-left"
    if suffix in UI and UI_PLACEHOLDER.search(text):
        yield "ui-placeholder"


def valid_waiver(text, marker=WAIVER):
    match = marker.search(text)
    if match and len(match[1].split()) >= 2:
        return match[1].strip()
    return ""


def deletion_reasons(added):
    """Deleted lines have nowhere to hold a waiver, so a new line in the same file carries it."""
    reasons = {}
    for name, lines in added.items():
        for _, text in lines:
            reason = valid_waiver(text, DELETION_WAIVER)
            if reason:
                reasons.setdefault(name, reason)
    return reasons


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
    found = [make_finding("assertion-deleted", f, n, t, True) for f, n, t in unmatched[:surplus]]
    return with_deletion_waivers(found, added)


def with_deletion_waivers(found, added):
    reasons = deletion_reasons(added)
    for finding in found:
        finding.reason = reasons.get(finding.file, "")
        finding.waived = bool(finding.reason)
    return found


def removed_ci_steps(added, removed):
    gone = [(f, n, t) for f, n, t in removed if CI_PATH.search(f) and TEST_COMMAND.search(t)]
    kept = sum(1 for f, lines in added.items() if CI_PATH.search(f)
               for _, t in lines if TEST_COMMAND.search(t))
    surplus = len(gone) - kept
    found = [make_finding("ci-step-removed", f, n, t, False) for f, n, t in gone[:max(surplus, 0)]]
    return with_deletion_waivers(found, added)


def ci_rules(text):
    if TEST_COMMAND.search(text) and CI_BYPASS.search(text):
        yield "ci-bypass"
    if CI_SOFT_FAIL.search(text):
        yield "ci-soft-fail"


def weakened_assertions(added, removed):
    """Warn when a file lost strict assertions and gained loose ones."""
    lost = Counter(f for f, _, t in removed if TEST_PATH.search(f) and STRICT_ASSERTION.search(t))
    found = []
    for name, lines in added.items():
        if not (TEST_PATH.search(name) and is_scanned(name)):
            continue
        loose = [(n, t) for n, t in lines if LOOSE_ASSERTION.search(t)]
        found += [make_finding("assertion-weakened", name, n, t, True) for n, t in loose[:lost[name]]]
    return found


def scan_added(added):
    found = []
    for name, lines in added.items():
        if CI_PATH.search(name):
            found += [make_finding(rule, name, line_no, text, False)
                      for line_no, text in lines for rule in ci_rules(text)]
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
    return (scan_added(added) + deleted_assertions(added, removed)
            + weakened_assertions(added, removed) + removed_ci_steps(added, removed))


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

def scanner_version():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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
            state["repos"][key] = {"base": head_or_empty(root), "scanner": scanner_version(),
                                   "known": [f.fingerprint for f in scan_repo(root)]}
        elif folder_is_walkable(key):
            state["repos"][key] = {"since": time.time() - MTIME_MARGIN_SECONDS,
                                   "scanner": scanner_version(),
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


def without_known(found, known):
    """Each recorded issue excuses one match, so a new copy of an old line is still new."""
    remaining = Counter(known)
    new = []
    for finding in found:
        if remaining[finding.fingerprint] > 0:
            remaining[finding.fingerprint] -= 1
        else:
            new.append(finding)
    return new


def tool_uses(transcript_path):
    with open(transcript_path, errors="replace") as log:
        for line in log:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            content = (event.get("message") or {}).get("content")
            if event.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        yield block.get("name"), block.get("input") or {}


def looks_like_test_run(command):
    if NOT_A_TEST_RUN.search(command):
        return False
    return bool(TEST_COMMAND.search(command) or TEST_WORDS.search(command))


def tests_are_stale(transcript_path):
    """True when code was edited and no test command ran after the last edit."""
    if not transcript_path or not Path(transcript_path).exists():
        return False
    last_edit = last_test = -1
    for index, (tool, details) in enumerate(tool_uses(transcript_path)):
        if tool in EDIT_TOOLS and Path(details.get("file_path", "")).suffix.lower() in CODE:
            last_edit = index
        elif tool == "Bash" and looks_like_test_run(details.get("command", "")):
            last_test = index
    return last_edit > last_test


def project_has_tests(root):
    tracked = git(root, "ls-files", check=False).stdout.splitlines() if repo_root(root) else []
    names = tracked or (relative for _, relative in code_files(root))
    return any(TEST_PATH.search(name) for name in names)


def absorb_scanner_update(entry, found):
    """New rules can flag lines the older snapshot never recorded, so the first stop
    after an update records them. Returns the note for the user, or None."""
    if entry.get("scanner") == scanner_version():
        return None
    entry["scanner"] = scanner_version()
    unrecorded = without_known(found, entry["known"])
    entry["known"] = entry["known"] + [f.fingerprint for f in unrecorded]
    if not unrecorded:
        return None
    lines = [f"Unchecked {f.describe()}" for f in unrecorded if f.tier == "block" and not f.waived]
    return "\n".join(["The scanner was updated during this session, so its new rules did "
                      "not block code already there. Review these yourself:", *lines])


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
        entry = state["repos"].setdefault(
            key, {"base": "HEAD", "known": [], "scanner": scanner_version()})
        found = scan_repo(root, comparison_base(root, entry))
    elif key in state["repos"]:
        entry = state["repos"][key]
        found = scan_folder(key, entry["since"])
    else:
        return 0
    update_note = absorb_scanner_update(entry, found)
    new = without_known(found, entry["known"])
    blocks = [f for f in new if f.tier == "block" and not f.waived]
    notes = notes_for_user(new) + ([update_note] if update_note else [])
    if tests_are_stale(payload.get("transcript_path")) and project_has_tests(key):
        notes.append("Warning: code was edited and the tests weren't run afterward.")

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
          "`no-shortcuts-ok: <reason>` on that line and tell the user why. For a test "
          "deletion that is truly intended, add a comment in that file with "
          "`no-shortcuts-deleted-ok: <reason>`.", file=sys.stderr)
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
