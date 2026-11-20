#!/usr/bin/env python3
"""Build a redacted PayPal income summary from local CSV exports.

This script is intentionally CSV-first and local-only. It does not call PayPal,
read credentials, print payer details, or write outside exports/private by
default.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INBOX = ROOT / "exports" / "private" / "paypal" / "inbox"
DEFAULT_REPORT_DIR = ROOT / "exports" / "private" / "paypal" / "reports"


@dataclass
class PayPalRow:
    txn_date: date
    currency: str
    gross: Decimal
    fee: Decimal
    net: Decimal
    status: str
    txn_type: str
    ref_hash: str


def _normalized(row: dict[str, str]) -> dict[str, str]:
    return {str(k).strip().lower(): (v or "").strip() for k, v in row.items()}


def _first(row: dict[str, str], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return default


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported PayPal date format: {value!r}")


def parse_money(value: str) -> Decimal:
    value = (value or "").strip()
    if not value:
        return Decimal("0")
    negative = value.startswith("(") and value.endswith(")")
    cleaned = value.strip("()").replace("$", "").replace(",", "").replace("USD", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        amount = Decimal("0")
    return -amount if negative else amount


def _hash_ref(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def parse_paypal_csv(path: Path) -> list[PayPalRow]:
    rows: list[PayPalRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = _normalized(raw)
            date_value = _first(row, ("date", "transaction date", "created date"))
            currency = _first(row, ("currency", "currency code"), "UNKNOWN").upper()
            gross = parse_money(_first(row, ("gross", "gross amount", "amount")))
            fee = parse_money(_first(row, ("fee", "fee amount")))
            net = parse_money(_first(row, ("net", "net amount"), str(gross + fee)))
            status = _first(row, ("status", "transaction status")).lower()
            txn_type = _first(row, ("type", "transaction type")).lower()
            ref = _first(row, ("transaction id", "transaction id ", "id", "reference tx id"))
            rows.append(
                PayPalRow(
                    txn_date=parse_date(date_value),
                    currency=currency,
                    gross=gross,
                    fee=fee,
                    net=net,
                    status=status,
                    txn_type=txn_type,
                    ref_hash=_hash_ref(ref),
                )
            )
    return rows


def filter_rows(rows: list[PayPalRow], since: date | None, until: date | None) -> list[PayPalRow]:
    filtered = []
    for row in rows:
        if since and row.txn_date < since:
            continue
        if until and row.txn_date > until:
            continue
        filtered.append(row)
    return filtered


def summarize(rows: list[PayPalRow]) -> dict[str, dict[str, Decimal | int]]:
    summary: dict[str, dict[str, Decimal | int]] = {}
    for row in rows:
        item = summary.setdefault(
            row.currency,
            {
                "rows": 0,
                "completed": 0,
                "gross_positive": Decimal("0"),
                "fees": Decimal("0"),
                "negative_adjustments": Decimal("0"),
                "net": Decimal("0"),
            },
        )
        item["rows"] = int(item["rows"]) + 1
        if row.status in ("completed", "complete", ""):
            item["completed"] = int(item["completed"]) + 1
        if row.gross > 0:
            item["gross_positive"] = Decimal(item["gross_positive"]) + row.gross
        if row.fee:
            item["fees"] = Decimal(item["fees"]) + row.fee
        if row.net < 0:
            item["negative_adjustments"] = Decimal(item["negative_adjustments"]) + row.net
        item["net"] = Decimal(item["net"]) + row.net
    return summary


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def format_report(rows: list[PayPalRow], sources: list[Path], since: date | None, until: date | None) -> str:
    if not rows:
        return "No PayPal rows matched the filters."
    dates = [row.txn_date for row in rows]
    start = since or min(dates)
    end = until or max(dates)
    lines = [
        "# PayPal Income Report",
        "",
        "Private local summary. Income tracking only; not tax advice.",
        "",
        f"Source files: {len(sources)}",
        f"Date range: {start.isoformat()} to {end.isoformat()}",
        f"Rows matched: {len(rows)}",
        "",
    ]
    for currency, item in sorted(summarize(rows).items()):
        lines.extend([
            f"## {currency}",
            f"- rows: {item['rows']}",
            f"- completed-like rows: {item['completed']}",
            f"- gross positive: {money(Decimal(item['gross_positive']))}",
            f"- fees: {money(Decimal(item['fees']))}",
            f"- negative adjustments: {money(Decimal(item['negative_adjustments']))}",
            f"- net: {money(Decimal(item['net']))}",
            "",
        ])
    lines.append("Raw payer names, emails, notes, item text, and transaction IDs are intentionally omitted.")
    return "\n".join(lines)


def discover_inputs(inputs: list[str], inbox: Path) -> list[Path]:
    paths = [Path(item) for item in inputs]
    if not paths:
        paths = sorted(inbox.glob("*.csv")) if inbox.exists() else []
    return paths


def write_report(report: str, output_dir: Path, rows: list[PayPalRow]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if rows:
        month = min(row.txn_date for row in rows).strftime("%Y-%m")
    else:
        month = datetime.now().strftime("%Y-%m")
    path = output_dir / f"{month}.md"
    path.write_text(report + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a redacted PayPal income report from CSV exports.")
    parser.add_argument("inputs", nargs="*", help="PayPal CSV file(s). Defaults to exports/private/paypal/inbox/*.csv")
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX), help="Private CSV inbox directory.")
    parser.add_argument("--output-dir", default=str(DEFAULT_REPORT_DIR), help="Private report output directory.")
    parser.add_argument("--since", help="Inclusive start date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive end date, YYYY-MM-DD.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only and write no report.")
    parser.add_argument("--stdout", action="store_true", help="Print the redacted report instead of only the path.")
    args = parser.parse_args(argv)

    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    sources = discover_inputs(args.inputs, Path(args.inbox))
    if not sources:
        print("No PayPal CSV files found.")
        return 1

    rows: list[PayPalRow] = []
    for source in sources:
        rows.extend(parse_paypal_csv(source))
    rows = filter_rows(rows, since, until)
    report = format_report(rows, sources, since, until)

    if args.dry_run:
        print(f"Matched rows: {len(rows)}")
        for currency, item in sorted(summarize(rows).items()):
            print(f"{currency}: rows={item['rows']} net={money(Decimal(item['net']))}")
        return 0

    if args.stdout:
        print(report)
        return 0

    path = write_report(report, Path(args.output_dir), rows)
    print(f"Wrote PayPal income report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
