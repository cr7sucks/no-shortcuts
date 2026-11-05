"""Tests for scan.py. Run: python3 test_scan.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan  # noqa: E402  (test-only path setup)


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.state = Path(self._tmp.name) / "state"
        git(self.repo, "init", "-q")
        self.write("README.md", "hi\n")
        self.commit()

    def write(self, name, body):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "x")

    def rules(self, name, body, tier=None):
        self.write(name, body)
        found = scan.scan_repo(self.repo)
        return {f.rule for f in found if not f.waived and (tier is None or f.tier == tier)}

    def hook(self, mode, payload=None, cwd=None):
        payload = {"session_id": "s1", "cwd": str(cwd or self.repo), **(payload or {})}
        env = {**os.environ, "NO_SHORTCUTS_STATE_DIR": str(self.state)}
        return subprocess.run(
            [sys.executable, str(HERE / "scan.py"), mode],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )


class Rules(RepoCase):
    """One hit and one near-miss per rule."""

    CASES = [
        ("stub", "a.py", "def f():\n    raise NotImplementedError\n", "def f():\n    raise ValueError('bad')\n"),
        ("stub", "a.js", "throw new Error('not implemented');\n", "throw new Error('not found');\n"),
        ("stub", "a.py", "# TODO: implement retry\n", "# TODO: tidy this name\n"),
        ("stub", "a.py", "# in a real implementation this would call the API\n", "# the real API is called here\n"),
        ("silenced-error", "a.js", "try { f() } catch (e) {}\n", "try { f() } catch (e) { report(e) }\n"),
        ("silenced-error", "a.js", "try {\n  f()\n} catch (e) {\n  // ignore\n}\n", "try {\n  f()\n} catch (e) {\n  report(e)\n}\n"),
        ("silenced-error", "a.js", "p.catch(() => {});\n", "p.catch((e) => log(e));\n"),
        ("silenced-error", "a.py", "try:\n    f()\nexcept:\n    pass\n", "try:\n    f()\nexcept KeyError:\n    pass\n"),
        ("silenced-error", "a.py", "try:\n    f()\nexcept Exception:\n    # whatever\n    pass\n", "try:\n    f()\nexcept Exception:\n    log()\n"),
        ("checker-suppressed", "a.ts", "// @ts-ignore\nf();\n", "// @ts-expect-nothing\nf();\n"),
        ("checker-suppressed", "a.ts", "/* eslint-disable */\n", "/* eslint is great */\n"),
        ("checker-suppressed", "a.py", "x = f()  # type: ignore\n", "x = f()  # type: int\n"),
        ("checker-suppressed", "a.py", "import os  # noqa\n", "import os  # noqa-free\n"),
        ("test-disabled", "a.test.js", "it.skip('x', () => {});\n", "it('x', () => { skip(); });\n"),
        ("test-disabled", "a.test.js", "describe.only('x', () => {});\n", "describe('only x', () => {});\n"),
        ("test-disabled", "a.test.js", "xit('x', () => {});\n", "exit(1);\n"),
        ("test-disabled", "test_a.py", "@pytest.mark.skip\ndef test_a(): ...\n", "@pytest.mark.parametrize('a', [1])\ndef test_a(a): ...\n"),
        ("test-disabled", "test_a.py", "@pytest.mark.xfail\ndef test_a(): ...\n", "def test_xfail_label(): ...\n"),
        ("test-disabled", "test_a.py", "@unittest.skip('x')\ndef test_a(self): ...\n", "def test_a(self): ...\n"),
        ("test-disabled", "test_a.py", "raise unittest.SkipTest('no server')\n", "raise unittest.TestCase.failureException\n"),
        ("test-disabled", "test_a.py", "self.skipTest('no server')\n", "self.fail('no server')\n"),
        ("test-disabled", "test_a.py", "pytest.skip('no server')\n", "pytest.fail('no server')\n"),
    ]

    def test_each_rule_hits_and_near_miss_does_not(self):
        for rule, name, hit, near in self.CASES:
            with self.subTest(rule=rule, hit=hit):
                self.assertIn(rule, self.rules(name, hit))
            with self.subTest(rule=rule, near=near):
                self.assertNotIn(rule, self.rules(name, near))

    def test_stub_allowed_in_test_files(self):
        self.assertNotIn("stub", self.rules("tests/test_a.py", "raise NotImplementedError\n"))

    def test_skip_outside_tests_ignored(self):
        self.assertNotIn("test-disabled", self.rules("query.js", "db.skip(10);\n"))

    def test_warn_rules_are_warn_tier(self):
        self.assertIn("as-any", self.rules("a.ts", "const x = y as any;\n", tier="warn"))
        self.assertIn("debug-left", self.rules("a.js", "console.log(x);\n", tier="warn"))
        self.assertIn("ui-placeholder", self.rules("a.html", "<p>Lorem ipsum dolor</p>\n", tier="warn"))

    def test_ui_placeholder_ignored_outside_ui_files(self):
        self.assertNotIn("ui-placeholder", self.rules("a.py", "URL = 'https://example.com'\n"))

    def test_targeted_suppressions_warn_but_blanket_ones_block(self):
        for name, line in [("a.py", "import x  # noqa: F401\n"),
                           ("b.py", "x = f()  # type: ignore[attr-defined]\n"),
                           ("c.ts", "// eslint-disable-next-line no-console\n")]:
            with self.subTest(line=line):
                self.assertIn("suppression-targeted", self.rules(name, line, tier="warn"))
                self.assertNotIn("checker-suppressed", self.rules(name, line))
        self.assertIn("checker-suppressed", self.rules("d.py", "x = f()  # type: ignore\n", tier="block"))

    def test_explained_empty_catch_warns_unexplained_blocks(self):
        for name, body in [("a.js", "try { k() } catch { /* process already gone */ }\n"),
                           ("b.js", "try {\n  k()\n} catch (e) {\n  // logging is best effort\n}\n"),
                           ("c.py", "try:\n    k()\nexcept Exception:\n    # process already gone\n    pass\n")]:
            with self.subTest(name=name):
                self.assertIn("silenced-explained", self.rules(name, body, tier="warn"))
                self.assertNotIn("silenced-error", self.rules(name, body))

    def test_non_code_and_vendored_files_skipped(self):
        self.assertEqual(self.rules("notes.md", "raise NotImplementedError\n"), set())
        self.assertEqual(self.rules("node_modules/x/a.js", "catch (e) {}\n"), set())


class Diff(RepoCase):
    def test_old_code_not_flagged(self):
        self.write("a.py", "raise NotImplementedError\n")
        self.commit()
        self.assertEqual(self.rules("a.py", "raise NotImplementedError\nx = 1\n"), set())

    def test_untracked_files_scanned(self):
        self.assertIn("stub", self.rules("new/b.py", "raise NotImplementedError\n"))

    def test_line_numbers_are_correct(self):
        self.write("a.py", "one\ntwo\n")
        self.commit()
        self.write("a.py", "one\ntwo\nthree\nraise NotImplementedError\n")
        found = [f for f in scan.scan_repo(self.repo) if f.rule == "stub"]
        self.assertEqual(found[0].line, 4)

    def test_deleted_assertion_flagged(self):
        self.write("test_a.py", "def test_a():\n    assert f() == 1\n    assert g() == 2\n")
        self.commit()
        self.assertIn("assertion-deleted", self.rules("test_a.py", "def test_a():\n    assert f() == 1\n"))

    def test_moved_assertion_not_flagged(self):
        self.write("test_a.py", "def test_a():\n    assert f() == 1\n")
        self.commit()
        (self.repo / "test_a.py").unlink()
        self.assertNotIn("assertion-deleted", self.rules("test_b.py", "def test_a():\n    assert f() == 1\n"))


class Waivers(RepoCase):
    def test_reasoned_waiver_clears_block_and_is_recorded(self):
        self.write("a.py", "raise NotImplementedError  # no-shortcuts-ok: abstract method, subclasses override\n")
        found = scan.scan_repo(self.repo)
        self.assertTrue(found[0].waived)

    def test_reasonless_waiver_is_rejected(self):
        for mark in ("# no-shortcuts-ok", "# no-shortcuts-ok:", "# no-shortcuts-ok: fine"):
            with self.subTest(mark=mark):
                self.write("a.py", f"raise NotImplementedError  {mark}\n")
                self.assertFalse(scan.scan_repo(self.repo)[0].waived)


class DeletionWaivers(RepoCase):
    BEFORE = "def test_a():\n    assert f() == 1\n    assert g() == 2\n"
    AFTER = "def test_a():\n    assert f() == 1\n"

    def setUp(self):
        super().setUp()
        self.write("test_a.py", self.BEFORE)
        self.commit()

    def deleted(self, body):
        self.write("test_a.py", body)
        return [f for f in scan.scan_repo(self.repo) if f.rule == "assertion-deleted"]

    def test_reasoned_marker_waives_and_keeps_reason(self):
        found = self.deleted(self.AFTER + "# no-shortcuts-deleted-ok: feature removed from the product\n")
        self.assertTrue(found[0].waived)
        self.assertIn("feature removed", found[0].reason)

    def test_marker_without_reason_does_not_waive(self):
        for mark in ("# no-shortcuts-deleted-ok:\n", "# no-shortcuts-deleted-ok: fine\n"):
            with self.subTest(mark=mark):
                self.assertFalse(self.deleted(self.AFTER + mark)[0].waived)

    def test_ordinary_waiver_does_not_excuse_a_deletion(self):
        found = self.deleted(self.AFTER + "# no-shortcuts-ok: feature removed from the product\n")
        self.assertFalse(found[0].waived)

    def test_marker_in_another_file_does_not_waive(self):
        self.write("test_b.py", "# no-shortcuts-deleted-ok: feature removed from the product\n")
        self.assertFalse(self.deleted(self.AFTER)[0].waived)

    def test_old_marker_does_not_waive_a_later_deletion(self):
        self.write("test_a.py", self.BEFORE + "# no-shortcuts-deleted-ok: feature removed from the product\n")
        self.commit()
        found = self.deleted(self.AFTER + "# no-shortcuts-deleted-ok: feature removed from the product\n")
        self.assertFalse(found[0].waived)


class Hook(RepoCase):
    def start(self):
        return self.hook("--session-start", {"source": "startup"})

    def test_session_start_prints_instruction(self):
        out = self.start()
        self.assertEqual(out.returncode, 0)
        self.assertIn("no-shortcuts", out.stdout)

    def test_clean_allows(self):
        self.start()
        self.assertEqual(self.hook("--stop").returncode, 0)

    def test_hit_blocks_with_location(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 2)
        self.assertIn("a.py:1", out.stderr)

    def test_outside_git_allows(self):
        outside = Path(self._tmp.name) / "plain"
        outside.mkdir()
        (outside / "a.py").write_text("raise NotImplementedError\n")
        self.assertEqual(self.hook("--stop", cwd=outside).returncode, 0)

    def test_preexisting_work_does_not_block(self):
        self.write("a.py", "raise NotImplementedError\n")
        self.start()
        self.assertEqual(self.hook("--stop").returncode, 0)

    def test_new_hit_still_blocks_beside_preexisting_work(self):
        self.write("a.py", "raise NotImplementedError\n")
        self.start()
        self.write("b.js", "p.catch(() => {});\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 2)
        self.assertIn("b.js", out.stderr)
        self.assertNotIn("a.py", out.stderr)

    def test_committing_does_not_dodge_the_scan(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        self.commit()
        self.assertEqual(self.hook("--stop").returncode, 2)

    def test_stuck_guard_releases_on_fourth_attempt_and_tells_user(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        codes = [self.hook("--stop").returncode for _ in range(4)]
        self.assertEqual(codes, [2, 2, 2, 0])

    def test_released_findings_do_not_reblock_next_turn(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        for _ in range(4):
            last = self.hook("--stop")
        again = self.hook("--stop")
        self.assertEqual(again.returncode, 0)
        self.assertIn("a.py:1", again.stdout)

    def test_changed_findings_reset_the_guard(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        for _ in range(3):
            self.hook("--stop")
        self.write("b.js", "p.catch(() => {});\n")
        self.assertEqual(self.hook("--stop").returncode, 2)

    def test_waiver_is_reported_to_user(self):
        self.start()
        self.write("a.py", "raise NotImplementedError  # no-shortcuts-ok: abstract method, subclasses override\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 0)
        self.assertIn("abstract method", json.loads(out.stdout)["systemMessage"])

    def test_warnings_shown_without_blocking(self):
        self.start()
        self.write("a.js", "console.log(1);\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 0)
        self.assertIn("debug-left", json.loads(out.stdout)["systemMessage"])

    def test_scanner_crash_fails_open_and_says_so(self):
        self.start()
        (self.state / "s1.json").write_text("not json")
        self.write("a.py", "raise NotImplementedError\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 0)
        self.assertIn("failed", json.loads(out.stdout)["systemMessage"])

    def test_session_id_cannot_escape_state_dir(self):
        self.hook("--session-start", {"session_id": "../../evil", "source": "startup"})
        self.assertFalse((Path(self._tmp.name) / "evil.json").exists())

    def test_new_duplicate_of_preexisting_line_blocks(self):
        self.write("a.py", "raise NotImplementedError\n")
        self.start()
        self.write("a.py", "raise NotImplementedError\nx = 1\nraise NotImplementedError\n")
        out = self.hook("--stop")
        self.assertEqual(out.returncode, 2)

    def test_resume_keeps_original_snapshot(self):
        self.start()
        self.write("a.py", "raise NotImplementedError\n")
        self.hook("--session-start", {"source": "compact"})
        self.assertEqual(self.hook("--stop").returncode, 2)


class PlainFolder(RepoCase):
    """Projects with no git history."""

    def setUp(self):
        super().setUp()
        self.plain = Path(self._tmp.name) / "plain"
        self.plain.mkdir()
        (self.plain / "old.py").write_text("raise NotImplementedError\n")

    def start(self):
        return self.hook("--session-start", {"source": "startup"}, cwd=self.plain)

    def stop(self):
        return self.hook("--stop", cwd=self.plain)

    def test_existing_issues_do_not_block(self):
        self.start()
        self.assertEqual(self.stop().returncode, 0)

    def test_new_file_blocks(self):
        self.start()
        (self.plain / "new.py").write_text("raise NotImplementedError\n")
        out = self.stop()
        self.assertEqual(out.returncode, 2)
        self.assertIn("new.py:1", out.stderr)

    def test_edit_to_existing_file_blocks_only_new_lines(self):
        self.start()
        (self.plain / "old.py").write_text("raise NotImplementedError\ntry:\n    f()\nexcept:\n    pass\n")
        out = self.stop()
        self.assertEqual(out.returncode, 2)
        self.assertIn("silenced-error", out.stderr)
        self.assertNotIn("[stub]", out.stderr)

    def test_stuck_guard_works(self):
        self.start()
        (self.plain / "new.py").write_text("raise NotImplementedError\n")
        self.assertEqual([self.stop().returncode for _ in range(4)], [2, 2, 2, 0])

    def test_no_snapshot_allows(self):
        (self.plain / "new.py").write_text("raise NotImplementedError\n")
        self.assertEqual(self.stop().returncode, 0)

    def test_new_duplicate_of_existing_line_blocks(self):
        self.start()
        (self.plain / "old.py").write_text("raise NotImplementedError\nraise NotImplementedError\n")
        self.assertEqual(self.stop().returncode, 2)

    def test_oversized_folder_is_skipped_and_user_told(self):
        for number in range(6):
            (self.plain / f"f{number}.py").write_text("x = 1\n")
        env = {"NO_SHORTCUTS_MAX_FILES": "3"}
        out = self.hook_env("--session-start", env)
        self.assertEqual(out.returncode, 0)
        (self.plain / "new.py").write_text("raise NotImplementedError\n")
        self.assertEqual(self.stop().returncode, 0)

    def test_home_folder_is_never_walked(self):
        out = self.hook_env("--session-start", {"HOME": str(self.plain)})
        self.assertEqual(out.returncode, 0)
        (self.plain / "new.py").write_text("raise NotImplementedError\n")
        self.assertEqual(self.stop().returncode, 0)

    def hook_env(self, mode, extra):
        payload = {"session_id": "s1", "cwd": str(self.plain), "source": "startup"}
        env = {**os.environ, "NO_SHORTCUTS_STATE_DIR": str(self.state), **extra}
        return subprocess.run([sys.executable, str(HERE / "scan.py"), mode],
                              input=json.dumps(payload), capture_output=True, text=True, env=env)


if __name__ == "__main__":
    unittest.main()
