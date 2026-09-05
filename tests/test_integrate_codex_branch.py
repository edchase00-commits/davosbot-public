import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrate_codex_branch",
    ROOT / "scripts" / "integrate_codex_branch.py",
)
integrator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = integrator
SPEC.loader.exec_module(integrator)


class IntegrateCodexBranchTests(unittest.TestCase):
    def test_safe_codex_branch_names(self):
        self.assertTrue(integrator.is_safe_codex_branch("codex/plain-task"))
        self.assertTrue(integrator.is_safe_codex_branch("codex/feature.fix_123"))
        self.assertFalse(integrator.is_safe_codex_branch("master"))
        self.assertFalse(integrator.is_safe_codex_branch("feature/plain-task"))
        self.assertFalse(integrator.is_safe_codex_branch("codex/../master"))
        self.assertFalse(integrator.is_safe_codex_branch("codex/bad:ref"))
        self.assertFalse(integrator.is_safe_codex_branch("codex/bad.lock"))

    def test_red_tier_path_classifier_blocks_runtime_sensitive_files(self):
        blocked = dict(
            integrator.blocked_paths(
                [
                    ".github/workflows/tests.yml",
                    "davosbot/permissions.py",
                    "davosbot/tools.py",
                    "davosbot/cleanup_runner.py",
                    "exports/private/report.md",
                    "docs/CODEX_MULTI_CHAT.md",
                    "tests/test_example.py",
                ]
            )
        )

        self.assertIn(".github/workflows/tests.yml", blocked)
        self.assertIn("davosbot/permissions.py", blocked)
        self.assertIn("davosbot/tools.py", blocked)
        self.assertIn("davosbot/cleanup_runner.py", blocked)
        self.assertIn("exports/private/report.md", blocked)
        self.assertNotIn("docs/CODEX_MULTI_CHAT.md", blocked)
        self.assertNotIn("tests/test_example.py", blocked)

    def test_red_tier_path_classifier_blocks_secret_keywords(self):
        self.assertTrue(integrator.red_tier_reason("docs/admin_password_rotation.md"))
        self.assertTrue(integrator.red_tier_reason("scripts/private_send_probe.py"))
        self.assertFalse(integrator.red_tier_reason("docs/CODEX_SYNC.md"))

    def test_parse_args_defaults_to_tests_dispatch(self):
        args = integrator.parse_args(["--branch", "codex/plain-task"])

        self.assertEqual(args.dispatch_workflow, "tests.yml")
        self.assertFalse(args.skip_dispatch)

    @mock.patch.object(integrator.time, "sleep")
    @mock.patch.object(integrator, "github_ref_sha", side_effect=["old-sha", "expected-sha"])
    def test_wait_for_github_ref_retries_until_expected_sha(self, ref_sha, sleep):
        integrator.wait_for_github_ref(
            "master",
            "expected-sha",
            token="token",
            repo="owner/repo",
            attempts=3,
            delay_seconds=0.25,
        )

        self.assertEqual(ref_sha.call_count, 2)
        sleep.assert_called_once_with(0.25)

    @mock.patch.object(integrator.time, "sleep")
    @mock.patch.object(integrator, "github_ref_sha", return_value="stale-sha")
    def test_wait_for_github_ref_fails_closed_when_ref_stays_stale(self, ref_sha, sleep):
        with self.assertRaisesRegex(RuntimeError, "did not reach expecte"):
            integrator.wait_for_github_ref(
                "master",
                "expected-sha",
                token="token",
                repo="owner/repo",
                attempts=2,
                delay_seconds=0.25,
            )

        self.assertEqual(ref_sha.call_count, 2)
        sleep.assert_called_once_with(0.25)

    @mock.patch.dict(
        integrator.os.environ,
        {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo"},
        clear=False,
    )
    @mock.patch.object(integrator, "wait_for_github_ref")
    @mock.patch.object(integrator.request, "urlopen")
    def test_dispatch_waits_for_expected_sha_before_starting_workflow(self, urlopen, wait_for_ref):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        urlopen.return_value = response

        integrator.dispatch_workflow("master", "tests.yml", expected_sha="expected-sha")

        wait_for_ref.assert_called_once_with(
            "master",
            "expected-sha",
            token="token",
            repo="owner/repo",
        )
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
