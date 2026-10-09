import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "code_metrics.py"
SPEC = importlib.util.spec_from_file_location("code_metrics", SCRIPT_PATH)
code_metrics = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(code_metrics)


class CodeMetricsTests(unittest.TestCase):
    def test_source_path_filter_excludes_generated_data(self):
        self.assertTrue(code_metrics.is_source_path("davosbot/main.py"))
        self.assertTrue(code_metrics.is_source_path("web-app/app/page.tsx"))
        self.assertFalse(code_metrics.is_source_path("web-app/public/data/rankings.json"))
        self.assertFalse(code_metrics.is_source_path("notes.txt"))

    def test_file_metrics_counts_lines_and_nonblank_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "sample.py"
            path.write_text("one\n\nthree\n", encoding="utf-8")

            rows = code_metrics.file_metrics(root, ["sample.py"])

        self.assertEqual(1, len(rows))
        self.assertEqual(3, rows[0]["lines"])
        self.assertEqual(2, rows[0]["nonblank_lines"])
        self.assertEqual("Python", rows[0]["language"])

    def test_parse_numstat_log_filters_non_source_files(self):
        raw = "\n".join(
            [
                "@@commit@@abc123\t2026-07-30T12:00:00+00:00\tRefactor code",
                "10\t2\tdavosbot/main.py",
                "4\t1\tartifact.bin",
                "",
                "@@commit@@def456\t2026-07-29T12:00:00+00:00\tUpdate site",
                "3\t0\tweb-app/app/page.tsx",
            ]
        )

        rows = code_metrics.parse_numstat_log(raw)

        self.assertEqual(2, len(rows))
        self.assertEqual(10, rows[0]["additions"])
        self.assertEqual(2, rows[0]["deletions"])
        self.assertEqual(1, rows[0]["files_changed"])
        self.assertEqual("web-app/app/page.tsx", rows[1]["files"][0]["path"])

    def test_render_text_surfaces_largest_files_and_hotspots(self):
        report = {
            "commit": "abcdef1234567890",
            "summary": {
                "files": 2,
                "lines": 120,
                "nonblank_lines": 100,
                "recent_commits": 2,
                "recent_additions": 15,
                "recent_deletions": 4,
            },
            "languages": {"Python": {"files": 2, "lines": 120, "nonblank_lines": 100}},
            "largest_files": [{"path": "davosbot/main.py", "lines": 100}],
            "recent_hotspots": [{"path": "davosbot/main.py", "churn": 19}],
        }

        rendered = code_metrics.render_text(report)

        self.assertIn("abcdef123456", rendered)
        self.assertIn("davosbot/main.py (100 lines)", rendered)
        self.assertIn("davosbot/main.py (19 changed lines)", rendered)


if __name__ == "__main__":
    unittest.main()
