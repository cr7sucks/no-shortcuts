# no-shortcuts

A [Claude Code](https://code.claude.com/docs) skill and blocking hook that stop Claude from faking finished work and help it avoid AI slop.

![How no-shortcuts works: Claude writes code, a scanner checks new changes when it tries to finish, shortcuts get blocked, and anything unresolved is shown to you](docs/how-it-works.svg)

A skill alone is only text Claude can skip. So this has two parts:

- **The skill** handles judgment, such as what to do when a check can't honestly pass.
- **The hooks** can't be skipped by forgetting. A scanner runs every time Claude tries to finish and refuses to let it stop while new shortcuts are in the code. A second check runs before package installs and stops names that don't exist.

## Install

Requires Claude Code and Python 3. Nothing else.

```bash
git clone https://github.com/cr7sucks/no-shortcuts.git
```

```bash
cd no-shortcuts && python3 install.py
```

It takes effect in your next session. Your settings are backed up to `~/.claude/settings.json.bak-no-shortcuts` first. To remove it:

```bash
python3 install.py --uninstall
```

### Cursor and Codex (experimental)

The same checks can run as hooks in Cursor and OpenAI Codex. Nothing changes in those tools unless you ask:

```bash
python3 install.py --agents cursor,codex
```

Your existing hooks are kept, and `--uninstall` removes only ours. Codex skips new hooks until you trust them, so run `/hooks` in Codex afterward.

These adapters follow each tool's hook documentation and pass tests that simulate those hooks. **They haven't been run inside the real apps yet.** Compared with Claude Code:

| | Claude Code | Cursor | Codex |
|---|---|---|---|
| Blocks finishing on new shortcuts | Yes | Yes, by sending a follow-up message | Yes |
| Blocks fake package installs | Yes | Yes | Yes |
| Tells the agent to read the rules | Yes | Yes | Yes |
| Shows you waivers and unresolved findings | Yes | Only in Cursor's hook log | Yes |
| Warns when tests weren't rerun | Yes | No | No |

In a Cursor workspace with several root folders, only the first is scanned. Cursor's session start doesn't wait for hooks, so the first edits of a session could happen before the snapshot is saved.

## What it checks

**Blocks Claude from finishing:**

| Rule | Example |
|---|---|
| Stubs in real code | `raise NotImplementedError`, `throw new Error("not implemented")`, `TODO: implement` |
| Disabled tests | `it.skip(`, `.only(`, `@pytest.mark.skip`, `unittest.SkipTest`, `self.skipTest(`, `@Disabled`, `XCTSkip` |
| Tests that can't fail | `assert True`, `expect(true).toBe(true)` |
| Deleted assertions | More assertions removed than added |
| CI bypasses | `pytest \|\| true`, `npm test; exit 0`, a removed test step |
| Silenced errors | Empty `catch {}`, `except: pass`, `.catch(() => {})`, Go's `_ = err`, Ruby's `rescue nil` |
| Blanket suppressions | `@ts-ignore`, `eslint-disable`, bare `# type: ignore` or `# noqa` |
| Fake packages | `npm install` or `pip install` of a name missing from the public registry |

**Only warns:** assertions swapped for looser ones, CI settings like `continue-on-error: true`, code edited with no test run afterward, targeted suppressions like `# noqa: F401`, empty catches with a stated reason, ignored `pcall` results in Luau, Rust's `.unwrap()`, `as any`, debug prints, and placeholder text in UI files.

Patterns cover Python, JavaScript and TypeScript, plus common cases in Go, Rust, Java, Kotlin, Swift, Ruby, Lua and Luau.

The scanner only looks at code changed since the session started, so existing code never blocks you. It works with or without git. Without git it checks whole changed files and can't detect deleted assertions.

## When a hit is correct

Add a waiver with a reason of at least two words on that line. Waived lines are always shown to you.

```python
raise NotImplementedError  # no-shortcuts-ok: abstract method, subclasses override
```

A deleted line has nowhere to hold a waiver, so an intended test deletion or removed CI step takes a comment in that same file:

```python
# no-shortcuts-deleted-ok: feature removed, its tests went with it
```

A package from a private registry can be waived the same way, by adding `# no-shortcuts-ok: <reason>` to the install command. Projects with `registry=` in `.npmrc`, or with `PIP_INDEX_URL` set, skip the package check.

If Claude is blocked 3 times on the same issues, it's allowed to stop and the unresolved issues are shown to you, so a bad rule can't trap a session.

## Does it work?

It was tested the way the skill itself asks: watch agents fail first, then check whether the skill changes that. Results come from each run's actual files, not its summary.

Six pressure scenarios ran without the skill. Five stayed clean, including 36 tedious handlers that passed 228 hidden checks. The one that failed was CI that couldn't honestly pass, with a customer down and the user unreachable. That scenario was then rerun:

| Reruns | Weakened tests to pass | Left CI red, handed over a tested patch | Hits the scanner would block |
|---|---|---|---|
| Without the skill (2) | 2 of 2 | 0 of 2 | Both caught |
| With the skill (3) | 0 of 3 | 3 of 3 | None |

## Limits

- **Small sample:** these are a handful of runs on one scenario, not proof.
- **Short tasks only:** long sessions weren't tested, and that's where rushing is most likely.
- **The slop checklists are unproven:** no run produced slop, so `code-slop.md` and `ui-slop.md` are research-based guidance and are not enforced.
- **Not tamper-proof:** Claude can edit files, so it could write a waiver or change the hook. Waivers are shown to you; a hook edit wouldn't be.
- **Pattern matching only:** the scanner finds clear shortcuts, not bad design.
- **Existing doesn't mean safe:** the package check only proves a name exists. Attackers register names that AI tools tend to invent, so still review unfamiliar packages. If the registry can't be reached, the install goes ahead and you're told.
- **Some languages are lighter:** only the patterns listed above are covered, and Luau and the other newer rules have seen far less testing.
- **Skipped folders:** your home folder, and non-git folders with more than 5,000 code files.

## Tests

```bash
python3 skill/no-shortcuts/scripts/test_scan.py
python3 skill/no-shortcuts/scripts/test_check_packages.py
python3 skill/no-shortcuts/scripts/test_bridge.py
python3 test_install.py
```

## License

[MIT](LICENSE)
