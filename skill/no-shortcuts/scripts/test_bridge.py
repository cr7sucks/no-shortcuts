"""Tests for bridge.py. Run: python3 test_bridge.py

Each expected shape comes from the vendor's hook documentation, not from a real
Cursor or Codex run.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bridge  # noqa: E402  (test-only path setup)

SHORTCUT = "raise NotImplementedError\n"


class BridgeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        self.state = Path(tmp.name) / "state"
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t",
                                      "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def run_bridge(self, agent, event, payload):
        env = {**os.environ, "NO_SHORTCUTS_STATE_DIR": str(self.state)}
        env.pop("CURSOR_PROJECT_DIR", None)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(HERE / "bridge.py"), agent, event],
                              input=text, capture_output=True, text=True, env=env, cwd=str(HERE))

    def add_shortcut(self):
        (self.repo / "a.py").write_text(SHORTCUT)


class Cursor(BridgeCase):
    def base(self, **extra):
        return {"conversation_id": "c1", "generation_id": "g1", "hook_event_name": "stop",
                "workspace_roots": [str(self.repo)], "transcript_path": "/ignored", **extra}

    def stop(self, **extra):
        return self.run_bridge("cursor", "stop", self.base(status="completed", loop_count=0, **extra))

    def test_session_start_adds_context_pointing_at_the_skill(self):
        out = self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        self.assertEqual(out.returncode, 0)
        context = json.loads(out.stdout)["additional_context"]
        self.assertIn(str(HERE.parent / "SKILL.md"), context)
        self.assertNotIn("load the no-shortcuts skill", context)

    def test_stop_uses_workspace_root_because_cursor_sends_no_cwd(self):
        self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        self.add_shortcut()
        out = self.stop()
        self.assertEqual(out.returncode, 0)
        self.assertIn("a.py:1", json.loads(out.stdout)["followup_message"])

    def test_stop_keeps_one_session_across_ids(self):
        self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        self.stop()
        self.assertEqual([p.name for p in self.state.glob("*.json")], ["c1.json"])

    def test_clean_stop_sends_no_followup(self):
        self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        out = self.stop()
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), {})

    def test_stop_ignores_turns_the_user_aborted_or_that_errored(self):
        self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        self.add_shortcut()
        for status in ("aborted", "error"):
            with self.subTest(status=status):
                out = self.run_bridge("cursor", "stop", self.base(status=status, loop_count=0))
                self.assertEqual(json.loads(out.stdout), {})

    def test_stuck_guard_releases_inside_cursors_loop_limit(self):
        self.run_bridge("cursor", "session-start", self.base(session_id="c1"))
        self.add_shortcut()
        followups = [json.loads(self.stop().stdout) for _ in range(4)]
        self.assertEqual(["followup_message" in f for f in followups], [True, True, True, False])

    def test_shell_allow_never_grants_permission(self):
        out = self.run_bridge("cursor", "shell", self.base(command="git status", cwd=str(self.repo)))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")

    def test_shell_block_denies_with_reason_for_agent_and_user(self):
        result = bridge.from_core("cursor", "shell", 2, "", "Not found on the public registry: x")
        code, stdout, _ = result
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {
            "permission": "deny",
            "user_message": "Not found on the public registry: x",
            "agent_message": "Not found on the public registry: x"})

    def test_shell_note_goes_to_the_log_not_the_reply(self):
        note = json.dumps({"systemMessage": "couldn't confirm x"})
        code, stdout, stderr = bridge.from_core("cursor", "shell", 0, note, "")
        self.assertEqual((code, stdout), (0, ""))
        self.assertIn("couldn't confirm x", stderr)

    def test_stop_note_goes_to_the_log(self):
        note = json.dumps({"systemMessage": "Waived a.py:1"})
        code, stdout, stderr = bridge.from_core("cursor", "stop", 0, note, "")
        self.assertEqual((code, json.loads(stdout)), (0, {}))
        self.assertIn("Waived a.py:1", stderr)

    def test_no_folder_at_all_does_nothing(self):
        out = self.run_bridge("cursor", "stop", {"conversation_id": "c1", "status": "completed"})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), {})


class Codex(BridgeCase):
    def base(self, **extra):
        return {"session_id": "x1", "cwd": str(self.repo), "transcript_path": None,
                "hook_event_name": "Stop", "model": "m", **extra}

    def test_session_start_is_plain_text_pointing_at_the_skill(self):
        out = self.run_bridge("codex", "session-start", self.base(source="startup"))
        self.assertEqual(out.returncode, 0)
        self.assertIn(str(HERE.parent / "SKILL.md"), out.stdout)
        with self.assertRaises(ValueError):
            json.loads(out.stdout)

    def test_stop_blocks_with_exit_2_and_reason_on_stderr(self):
        self.run_bridge("codex", "session-start", self.base(source="startup"))
        self.add_shortcut()
        out = self.run_bridge("codex", "stop", self.base(stop_hook_active=False))
        self.assertEqual(out.returncode, 2)
        self.assertIn("a.py:1", out.stderr)

    def test_clean_stop_prints_valid_json(self):
        self.run_bridge("codex", "session-start", self.base(source="startup"))
        out = self.run_bridge("codex", "stop", self.base(stop_hook_active=False))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), {"continue": True})

    def test_stuck_guard_releases_even_though_codex_marks_continued_turns(self):
        self.run_bridge("codex", "session-start", self.base(source="startup"))
        self.add_shortcut()
        codes = [self.run_bridge("codex", "stop", self.base(stop_hook_active=True)).returncode
                 for _ in range(4)]
        self.assertEqual(codes, [2, 2, 2, 0])

    def test_released_stop_shows_the_user_what_is_unresolved(self):
        self.run_bridge("codex", "session-start", self.base(source="startup"))
        self.add_shortcut()
        for _ in range(3):
            self.run_bridge("codex", "stop", self.base(stop_hook_active=True))
        out = self.run_bridge("codex", "stop", self.base(stop_hook_active=True))
        self.assertIn("a.py:1", json.loads(out.stdout)["systemMessage"])

    def test_pre_tool_allows_a_normal_command(self):
        payload = self.base(tool_name="Bash", tool_input={"command": "git status"}, tool_use_id="t")
        out = self.run_bridge("codex", "pre-tool", payload)
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_pre_tool_block_keeps_exit_2_and_reason(self):
        self.assertEqual(bridge.from_core("codex", "pre-tool", 2, "", "Not found: x"),
                         (2, "", "Not found: x"))

    def test_pre_tool_note_stays_json_because_codex_ignores_plain_text(self):
        note = json.dumps({"systemMessage": "couldn't confirm x"})
        self.assertEqual(bridge.from_core("codex", "pre-tool", 0, note, ""), (0, note, ""))


class Failure(BridgeCase):
    def test_bad_input_never_traps_a_session(self):
        for agent, event in (("cursor", "stop"), ("cursor", "shell"), ("cursor", "session-start"),
                             ("codex", "stop"), ("codex", "pre-tool"), ("codex", "session-start")):
            with self.subTest(agent=agent, event=event):
                out = self.run_bridge(agent, event, "not json")
                self.assertEqual(out.returncode, 0)
                self.assertIn("did not check", out.stderr)

    def test_failed_stop_still_prints_the_shape_each_tool_requires(self):
        self.assertEqual(json.loads(self.run_bridge("cursor", "stop", "not json").stdout), {})
        message = json.loads(self.run_bridge("codex", "stop", "not json").stdout)
        self.assertIn("did not check", message["systemMessage"])

    def test_failed_shell_check_never_grants_permission(self):
        self.assertEqual(self.run_bridge("cursor", "shell", "not json").stdout, "")

    def test_unknown_agent_or_event_is_an_error_not_a_silent_pass(self):
        out = self.run_bridge("windsurf", "stop", {})
        self.assertEqual(out.returncode, 1)
        self.assertIn("usage", out.stderr.lower())


if __name__ == "__main__":
    unittest.main()
