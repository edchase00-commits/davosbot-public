import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("quality_sweep", ROOT / "scripts" / "quality_sweep.py")
quality_sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quality_sweep
assert SPEC.loader is not None
SPEC.loader.exec_module(quality_sweep)


class QualitySweepTests(unittest.TestCase):
    def test_bounded_report_retains_start_and_failure_tail(self):
        text = "Committed snapshot: synthetic\n" + "progress\n" * 1000 + "Final stage: FAIL"
        clean = quality_sweep._clean_output(text, limit=300)
        self.assertEqual(300, len(clean))
        self.assertTrue(clean.startswith("Committed snapshot: synthetic"))
        self.assertTrue(clean.endswith("Final stage: FAIL"))
        self.assertIn("middle omitted", clean)

    def test_full_checks_use_external_committed_snapshot_runner(self):
        with patch.object(quality_sweep, "_project_python", return_value="synthetic-python"), patch.object(
            quality_sweep, "_run", return_value=quality_sweep.SweepResult("synthetic", "FAIL", "exit 124"),
        ) as run:
            result = quality_sweep.check_full_validate()
        self.assertEqual("FAIL", result.status)
        self.assertEqual(["synthetic-python", "scripts/review_validation.py", "--timeout", "650"], run.call_args.args[0])

    def test_light_report_cannot_overwrite_full_completion_or_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(quality_sweep, "REPORT_DIR", Path(tmp)):
            quality_sweep.write_report([quality_sweep.SweepResult("validation_agent", "FAIL", "timeout")], mode="full")
            full_path = Path(tmp) / "quality_sweep_full_state.json"
            before = full_path.read_text()
            quality_sweep.write_report([quality_sweep.SweepResult("repo_guard_agent", "PASS", "ok")], mode="light")
            self.assertEqual(before, full_path.read_text())
            self.assertFalse(json.loads(before)["ok"])
            self.assertTrue(json.loads((Path(tmp) / "quality_sweep_light_state.json").read_text())["ok"])
            self.assertIn("FAIL", (Path(tmp) / "quality_sweep_full.md").read_text())

    def test_agent_plan_has_light_and_full_checks(self):
        agents = {agent.name: set(agent.modes) for agent in quality_sweep.build_agents()}

        self.assertIn("light", agents["repo_guard_agent"])
        self.assertIn("light", agents["quick_smoke_agent"])
        self.assertIn("light", agents["maintenance_agent"])
        self.assertIn("light", agents["alert_audit_agent"])
        self.assertIn("light", agents["queue_agent"])
        self.assertIn("full", agents["validation_agent"])
        self.assertIn("full", agents["public_snapshot_agent"])
        self.assertIn("full", agents["runtime_smoke_agent"])

    def test_alert_audit_passes_when_wait_alert_is_gated(self):
        with patch.object(quality_sweep, "_installed_crontab_text", return_value=""):
            result = quality_sweep.check_alert_audit()

        self.assertEqual("PASS", result.status)
        self.assertIn("normal CI waiting is silent", result.detail)

    def test_alert_audit_flags_legacy_empty_commit_cron(self):
        crontab = "0 2 * * * cd ~/projects/davosbot && git add MEMORY.md && git commit -m \"auto: memory backup\" --allow-empty && git push"

        issues = quality_sweep._noisy_cron_issues(crontab)

        self.assertIn("legacy MEMORY.md backup cron can create empty daily commits", issues)

    def test_quick_smoke_uses_resolved_project_python(self):
        with patch.object(quality_sweep, "_project_python", return_value="/tmp/davos-python"), patch.object(
            quality_sweep,
            "_run",
            return_value=quality_sweep.SweepResult("quick_smoke_agent", "PASS", "ok"),
        ) as run_mock:
            result = quality_sweep.check_quick_smoke()

        self.assertEqual("PASS", result.status)
        self.assertEqual(["/tmp/davos-python", "scripts/codex_operator.py", "run", "quick_smoke"], run_mock.call_args[0][0])

    def test_record_failure_change_log_dedupes_same_fingerprint(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "davosbot.db"
            state_path = Path(tmp) / "state.json"
            report = Path(tmp) / "quality_sweep.md"
            report.write_text("report", encoding="utf-8")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
            results = [
                quality_sweep.SweepResult("repo_guard_agent", "PASS", "ok"),
                quality_sweep.SweepResult("quick_smoke_agent", "FAIL", "broken"),
            ]

            first = quality_sweep.record_failure_change_log(results, report, state_path, db_path)
            second = quality_sweep.record_failure_change_log(results, report, state_path, db_path)

            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
                request = conn.execute("SELECT request FROM change_log").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(1, first)
        self.assertIsNone(second)
        self.assertEqual(1, count)
        self.assertIn("[QUALITY-SWEEP YELLOW]", request)

    def test_write_report_marks_failures_without_secret_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(quality_sweep, "REPORT_DIR", Path(tmp)):
                report = quality_sweep.write_report(
                    [
                        quality_sweep.SweepResult("repo_guard_agent", "PASS", "ok"),
                        quality_sweep.SweepResult("alert_audit_agent", "FAIL", "token=abc123"),
                    ],
                    mode="light",
                )

            text = report.read_text(encoding="utf-8")

        self.assertIn("Overall: FAIL", text)
        self.assertIn("alert_audit_agent", text)
        self.assertIn("token=[redacted]", text)


if __name__ == "__main__":
    unittest.main()
