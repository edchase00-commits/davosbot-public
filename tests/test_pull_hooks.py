import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _load_pull_helpers(namespace_overrides):
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    wanted = {"_ensure_git_hooks_path", "_cmd_pull"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Path": Path,
        "logger": SimpleNamespace(warning=lambda *args, **kwargs: None),
    }
    namespace.update(namespace_overrides)
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace["_cmd_pull"]


class PullHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        hook_dir = self.project / ".githooks"
        hook_dir.mkdir()
        (hook_dir / "post-merge").write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pull_installs_hooks_uses_ff_only_and_restarts_after_success(self):
        calls = []
        popen_calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "config", "core.hooksPath"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "pull", "--ff-only"]:
                return SimpleNamespace(returncode=0, stdout="Updating abc..def\nFast-forward", stderr="")
            if args[:3] == ["git", "log", "-1"]:
                return SimpleNamespace(returncode=0, stdout="def1234 - test commit", stderr="")
            raise AssertionError(f"unexpected subprocess call: {args}")

        subprocess_stub = SimpleNamespace(
            run=fake_run,
            Popen=lambda args: popen_calls.append(args),
        )
        pull = _load_pull_helpers({
            "_PROJECT_DIR": str(self.project),
            "check_action_permission": lambda sender, action: None,
            "subprocess": subprocess_stub,
        })

        reply = pull("owner")

        self.assertIn(["git", "config", "core.hooksPath", ".githooks"], calls)
        self.assertIn(["git", "pull", "--ff-only"], calls)
        self.assertIn("hooks: .githooks active", reply)
        self.assertIn("restarting", reply)
        self.assertEqual(1, len(popen_calls))

    def test_pull_failure_does_not_restart(self):
        calls = []
        popen_calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "config", "core.hooksPath"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:3] == ["git", "pull", "--ff-only"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="fatal: Not possible to fast-forward")
            raise AssertionError(f"unexpected subprocess call: {args}")

        subprocess_stub = SimpleNamespace(
            run=fake_run,
            Popen=lambda args: popen_calls.append(args),
        )
        pull = _load_pull_helpers({
            "_PROJECT_DIR": str(self.project),
            "check_action_permission": lambda sender, action: None,
            "subprocess": subprocess_stub,
        })

        reply = pull("owner")

        self.assertIn("pull failed", reply)
        self.assertIn("Not possible to fast-forward", reply)
        self.assertFalse(any(call[:3] == ["git", "log", "-1"] for call in calls))
        self.assertEqual([], popen_calls)


if __name__ == "__main__":
    unittest.main()
