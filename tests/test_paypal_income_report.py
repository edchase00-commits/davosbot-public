import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paypal_income_report.py"
spec = importlib.util.spec_from_file_location("paypal_income_report", SCRIPT)
paypal_income_report = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["paypal_income_report"] = paypal_income_report
spec.loader.exec_module(paypal_income_report)


CSV_TEXT = """Date,Name,Type,Status,Currency,Gross,Fee,Net,From Email Address,Transaction ID,Note
2026-05-01,Jane Customer,Payment Received,Completed,USD,100.00,-3.20,96.80,jane@example.com,TXN-SECRET-1,private note
2026-05-02,Refund Person,Refund,Completed,USD,-20.00,0.00,-20.00,refund@example.com,TXN-SECRET-2,refund note
2026-05-03,Ignored Person,Payment Received,Pending,USD,50.00,-1.75,48.25,ignored@example.com,TXN-SECRET-3,pending note
"""


class PayPalIncomeReportTests(unittest.TestCase):
    def test_report_summarizes_without_private_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "paypal.csv"
            csv_path.write_text(CSV_TEXT, encoding="utf-8")

            rows = paypal_income_report.parse_paypal_csv(csv_path)
            report = paypal_income_report.format_report(rows, [csv_path], None, None)

        self.assertIn("PayPal Income Report", report)
        self.assertIn("Rows matched: 3", report)
        self.assertIn("gross positive: 150.00", report)
        self.assertIn("fees: -4.95", report)
        self.assertIn("negative adjustments: -20.00", report)
        self.assertIn("net: 125.05", report)
        self.assertNotIn("Jane Customer", report)
        self.assertNotIn("jane@example.com", report)
        self.assertNotIn("TXN-SECRET", report)
        self.assertNotIn("private note", report)

    def test_dry_run_prints_counts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "paypal.csv"
            csv_path.write_text(CSV_TEXT, encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = paypal_income_report.main([str(csv_path), "--dry-run", "--since", "2026-05-01", "--until", "2026-05-02"])

        output = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Matched rows: 2", output)
        self.assertIn("USD: rows=2 net=76.80", output)
        self.assertNotIn("Jane Customer", output)
        self.assertNotIn("jane@example.com", output)
        self.assertNotIn("TXN-SECRET", output)

    def test_writes_report_to_private_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "paypal.csv"
            out_dir = tmp_path / "reports"
            csv_path.write_text(CSV_TEXT, encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                code = paypal_income_report.main([str(csv_path), "--output-dir", str(out_dir)])

            report_path = out_dir / "2026-05.md"
            report = report_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(report_path.name.endswith(".md"))
        self.assertIn("PayPal Income Report", report)
        self.assertNotIn("example.com", report)


if __name__ == "__main__":
    unittest.main()
