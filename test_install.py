"""Tests for install.py, run against a temporary Claude folder."""
import json
import tempfile
import unittest
from pathlib import Path

import install


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
        self.assertEqual(sorted(self.read()["hooks"]), ["SessionStart", "Stop"])

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
