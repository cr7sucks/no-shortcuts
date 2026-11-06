"""Tests for install.py, run against a temporary Claude folder."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import install


class OtherAgents(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / ".claude"
        self.cursor = self.root / ".cursor/hooks.json"
        self.codex = self.root / ".codex/hooks.json"

    def install(self, *agents):
        install.install(self.claude, agents, self.root)

    def bridge_command(self, *args):
        script = (self.claude / "skills/no-shortcuts/scripts/bridge.py").as_posix()
        return f'python3 "{script}" ' + " ".join(args)

    def test_default_install_leaves_other_agents_alone(self):
        self.install()
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.codex.exists())

    def test_cursor_hooks_match_its_config_shape(self):
        self.install("cursor")
        saved = json.loads(self.cursor.read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["hooks"], {
            "sessionStart": [{"command": self.bridge_command("cursor", "session-start"), "timeout": 30}],
            "beforeShellExecution": [{"command": self.bridge_command("cursor", "shell"), "timeout": 30}],
            "stop": [{"command": self.bridge_command("cursor", "stop"), "timeout": 30}]})

    def test_codex_hooks_match_its_config_shape(self):
        self.install("codex")
        hooks = json.loads(self.codex.read_text())["hooks"]
        self.assertEqual(sorted(hooks), ["PreToolUse", "SessionStart", "Stop"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(hooks["Stop"][0]["hooks"], [
            {"type": "command", "command": self.bridge_command("codex", "stop"), "timeout": 30}])

    def test_other_tools_hooks_are_kept(self):
        self.cursor.parent.mkdir()
        mine = {"command": "./mine.sh"}
        self.cursor.write_text(json.dumps({"version": 1, "hooks": {"stop": [mine]}}))
        self.codex.parent.mkdir()
        theirs = {"hooks": [{"type": "command", "command": "echo theirs"}]}
        self.codex.write_text(json.dumps({"description": "d", "hooks": {"Stop": [theirs]}}))
        self.install("cursor", "codex")
        self.install("cursor", "codex")
        cursor = json.loads(self.cursor.read_text())
        codex = json.loads(self.codex.read_text())
        self.assertIn(mine, cursor["hooks"]["stop"])
        self.assertEqual(len(cursor["hooks"]["stop"]), 2)
        self.assertEqual(codex["description"], "d")
        self.assertIn(theirs, codex["hooks"]["Stop"])
        self.assertEqual(len(codex["hooks"]["Stop"]), 2)

    def test_uninstall_removes_only_our_hooks(self):
        self.cursor.parent.mkdir()
        mine = {"command": "./mine.sh"}
        self.cursor.write_text(json.dumps({"version": 1, "hooks": {"stop": [mine]}}))
        self.install("cursor", "codex")
        install.uninstall(self.claude, self.root)
        self.assertEqual(json.loads(self.cursor.read_text())["hooks"], {"stop": [mine]})
        self.assertEqual(json.loads(self.codex.read_text())["hooks"], {})

    def test_uninstall_creates_nothing_for_agents_never_installed(self):
        self.install()
        install.uninstall(self.claude, self.root)
        self.assertFalse(self.cursor.exists())
        self.assertFalse(self.codex.exists())

    def test_broken_config_changes_nothing(self):
        self.cursor.parent.mkdir()
        self.cursor.write_text("{not json")
        with self.assertRaises(SystemExit):
            self.install("cursor")
        self.assertEqual(self.cursor.read_text(), "{not json")

    def test_codex_install_says_hooks_need_trust(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.install("codex")
        self.assertIn("/hooks", out.getvalue())

    def test_agents_flag_is_parsed_and_checked(self):
        self.assertEqual(install.agents_from(["install.py"]), [])
        self.assertEqual(install.agents_from(["install.py", "--agents", "cursor,codex"]),
                         ["cursor", "codex"])
        with self.assertRaises(SystemExit):
            install.agents_from(["install.py", "--agents", "windsurf"])


class Installer(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.claude = Path(tmp.name)
        self.settings = self.claude / "settings.json"

    def read(self):
        return json.loads(self.settings.read_text())

    def test_fresh_install_adds_skill_and_hooks(self):
        install.install(self.claude)
        self.assertTrue((self.claude / "skills/no-shortcuts/scripts/scan.py").exists())
        hooks = self.read()["hooks"]
        self.assertEqual(sorted(hooks), ["PreToolUse", "SessionStart", "Stop"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
        self.assertTrue((self.claude / "skills/no-shortcuts/scripts/check_packages.py").exists())

    def test_existing_settings_and_hooks_are_kept(self):
        mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
        self.settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [mine]}}))
        install.install(self.claude)
        saved = self.read()
        self.assertEqual(saved["theme"], "dark")
        self.assertIn(mine, saved["hooks"]["Stop"])
        self.assertEqual(len(saved["hooks"]["Stop"]), 2)

    def test_installing_twice_does_not_duplicate_hooks(self):
        install.install(self.claude)
        install.install(self.claude)
        self.assertEqual(len(self.read()["hooks"]["Stop"]), 1)

    def test_upgrading_an_older_install_replaces_its_hooks(self):
        old = {"hooks": [{"type": "command", "command": 'python3 "$HOME/.claude/skills/no-shortcuts/scripts/scan.py" --stop'}]}
        self.settings.write_text(json.dumps({"hooks": {"Stop": [old]}}))
        install.install(self.claude)
        self.assertEqual(len(self.read()["hooks"]["Stop"]), 1)

    def test_uninstall_removes_only_ours(self):
        mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
        self.settings.write_text(json.dumps({"hooks": {"Stop": [mine]}}))
        install.install(self.claude)
        install.uninstall(self.claude)
        self.assertEqual(self.read()["hooks"], {"Stop": [mine]})
        self.assertFalse((self.claude / "skills/no-shortcuts").exists())

    def test_broken_settings_are_left_untouched(self):
        self.settings.write_text("{not json")
        with self.assertRaises(SystemExit):
            install.install(self.claude)
        self.assertEqual(self.settings.read_text(), "{not json")
        self.assertFalse((self.claude / "skills/no-shortcuts").exists())


if __name__ == "__main__":
    unittest.main()
