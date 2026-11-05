"""Tests for check_packages.py. No network: registry answers are faked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_packages as cp  # noqa: E402  (test-only path setup)

NPM, PYPI = "npm", "pypi"


class Parsing(unittest.TestCase):
    CASES = [
        ("npm install left-pad", [(NPM, "left-pad")]),
        ("npm i lodash@4.17.21 express", [(NPM, "lodash"), (NPM, "express")]),
        ("npm install -D @types/node@20", [(NPM, "@types/node")]),
        ("pnpm add zod", [(NPM, "zod")]),
        ("yarn add react", [(NPM, "react")]),
        ("bun add hono", [(NPM, "hono")]),
        ("pip install requests", [(PYPI, "requests")]),
        ("pip3 install 'flask[async]>=3.0' rich==13.0", [(PYPI, "flask"), (PYPI, "rich")]),
        ("python3 -m pip install -U httpx", [(PYPI, "httpx")]),
        ("uv add polars", [(PYPI, "polars")]),
        ("uv pip install numpy", [(PYPI, "numpy")]),
        ("poetry add pandas", [(PYPI, "pandas")]),
        ("cd api && npm install cors && npm test", [(NPM, "cors")]),
        ("FOO=1 sudo pip install black", [(PYPI, "black")]),
        ("npm install left-pad 2>&1", [(NPM, "left-pad")]),
        ("npm install left-pad > out.log 2>&1", [(NPM, "left-pad")]),
        ("pip install rich 2>/dev/null", [(PYPI, "rich")]),
        ("npm install left-pad &", [(NPM, "left-pad")]),
        ("npm i left-pad 2>&1 | tail -3", [(NPM, "left-pad")]),
    ]
    NOT_CHECKED = [
        "npm install", "npm ci", "npm test", "pip install -r requirements.txt", "pip install .",
        "pip install -e .", "pip install ./pkg.whl", "pip install git+https://github.com/a/b.git",
        "npm install ./local", "npm install github-user/repo", "npm install file:../x",
        "npm install x --registry https://npm.example.test", "pip install x -i https://pypi.example.test",
        "pip install x --index-url https://pypi.example.test/simple",
        "echo npm install fake-name", "git commit -m 'pip install stuff'", "npm install 'unclosed",
    ]

    def test_finds_packages(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                self.assertEqual(cp.packages_in(command), expected)

    def test_leaves_other_commands_alone(self):
        for command in self.NOT_CHECKED:
            with self.subTest(command=command):
                self.assertEqual(cp.packages_in(command), [])


class Deciding(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        self.env = {}

    def decide(self, command, lookup, tool="Bash"):
        payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(self.cwd)}
        return cp.decide(payload, lookup, self.env)

    def test_real_package_allowed_silently(self):
        self.assertEqual(self.decide("npm i real", lambda *_: True), (0, "", ""))

    def test_missing_package_blocked_with_name(self):
        code, reason, _ = self.decide("pip install fakepkg", lambda *_: False)
        self.assertEqual(code, 2)
        self.assertIn("fakepkg", reason)

    def test_one_missing_among_real_is_blocked(self):
        lookup = lambda _, name: name != "fakepkg"
        code, reason, _ = self.decide("npm i real fakepkg", lookup)
        self.assertEqual(code, 2)
        self.assertIn("fakepkg", reason)
        self.assertNotIn("real", reason.split("\n")[0])

    def test_unreachable_registry_allows_and_tells_user(self):
        code, _, note = self.decide("npm i maybe", lambda *_: None)
        self.assertEqual(code, 0)
        self.assertIn("maybe", note)

    def test_reasoned_waiver_allows_and_tells_user(self):
        command = "npm i internal-pkg  # no-shortcuts-ok: private package on company registry"
        code, _, note = self.decide(command, lambda *_: False)
        self.assertEqual(code, 0)
        self.assertIn("private package", note)

    def test_reasonless_waiver_is_rejected(self):
        code, _, _ = self.decide("npm i fakepkg  # no-shortcuts-ok: fine", lambda *_: False)
        self.assertEqual(code, 2)

    def test_project_with_private_npm_registry_is_skipped(self):
        (self.cwd / ".npmrc").write_text("registry=https://npm.example.test/\n")
        self.assertEqual(self.decide("npm i fakepkg", lambda *_: False)[0], 0)
        self.assertEqual(self.decide("pip install fakepkg", lambda *_: False)[0], 2)

    def test_private_pip_index_is_skipped(self):
        self.env["PIP_INDEX_URL"] = "https://pypi.example.test/simple"
        self.assertEqual(self.decide("pip install fakepkg", lambda *_: False)[0], 0)

    def test_other_tools_ignored(self):
        self.assertEqual(self.decide("npm i fakepkg", lambda *_: False, tool="Write")[0], 0)


class Registry(unittest.TestCase):
    def test_scoped_name_is_encoded(self):
        self.assertTrue(cp.registry_url(NPM, "@types/node").endswith("/@types%2Fnode"))
        self.assertEqual(cp.registry_url(PYPI, "flask"), "https://pypi.org/pypi/flask/json")


class Hook(unittest.TestCase):
    def test_crash_allows_and_says_so(self):
        code, _, note = cp.run("not json", cp.lookup_package, {})
        self.assertEqual(code, 0)
        self.assertIn("failed", json.loads(note)["systemMessage"])


if __name__ == "__main__":
    unittest.main()
