"""Read-only American-odds arithmetic, with no betting or account operations."""

from decimal import Decimal, InvalidOperation
import re


def positive_amount(value: Decimal | float | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Use a positive finite amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Use a positive finite amount.")
    return amount


def stake_for_profit(odds: int, profit: Decimal | float | str) -> Decimal:
    """Return risk needed for the requested net profit, excluding returned risk."""
    if not isinstance(odds, int) or isinstance(odds, bool) or abs(odds) < 100:
        raise ValueError("Use American odds of -100 or lower, or +100 or higher.")
    target = positive_amount(profit)
    if odds < 0:
        return target * Decimal(abs(odds)) / Decimal(100)
    return target * Decimal(100) / Decimal(odds)


_NUMBER = r"(?:[+-]?(?:\d+(?:\.\d+)?|\.\d+))"
_AMOUNT = rf"(?:\$\s*(?P<money>{_NUMBER})|(?P<units>{_NUMBER})\s*(?:u|units?))"
_ODDS = r"(?P<odds>[+-]\d{1,7})"
_PREFIX = (
    r"(?:(?:please|pls)\s+)?"
    r"(?:(?:calculate|calc)(?:\s+(?:risk|stake))?\s*:?\s*|"
    r"how\s+much(?:\s+(?:do|would|should)\s+i\s+(?:risk|stake|bet))?\s*)?"
)
_CALCULATION_PATTERNS = (
    re.compile(rf"{_PREFIX}(?:at\s+)?{_ODDS}\s+to\s+win\s*{_AMOUNT}[.?!]?", re.IGNORECASE),
    re.compile(rf"{_PREFIX}to\s+win\s*{_AMOUNT}\s+at\s+{_ODDS}[.?!]?", re.IGNORECASE),
)


def _format_amount(amount: Decimal, *, dollars: bool) -> str:
    rounded = amount.quantize(Decimal("0.01" if dollars else "0.0001"))
    approximate = "~" if rounded != amount else ""
    if rounded == 0 and amount > 0:
        number = f"{amount:.4g}"
    else:
        number = f"{rounded:.2f}" if dollars else format(rounded, "f").rstrip("0").rstrip(".")
    return f"{approximate}${number}" if dollars else f"{approximate}{number}u"


def calculation_reply(text: str) -> str | None:
    """Answer only a complete, explicit odds/target-profit calculation.

    A full match prevents swallowing an instruction to log, send, settle, or
    otherwise act. The caller retains existing routes for all other requests.
    """
    clean = re.sub(r"\s+", " ", (text or "").strip()).replace("\u2212", "-")
    if len(clean) > 200:
        return None
    match = next((m for pattern in _CALCULATION_PATTERNS if (m := pattern.fullmatch(clean))), None)
    if match is None:
        return None
    dollars = match.group("money") is not None
    try:
        odds = int(match.group("odds"))
        profit = positive_amount(match.group("money") if dollars else match.group("units"))
        stake = stake_for_profit(odds, profit)
        risk_text = _format_amount(stake, dollars=dollars)
        profit_text = _format_amount(profit, dollars=dollars)
        return_text = _format_amount(stake + profit, dollars=dollars)
    except (ValueError, InvalidOperation):
        return "Use valid American odds (for example -125 or +200) and a positive amount to win, such as 1u or $100."
    return (
        f"At {odds:+d}: risk {risk_text} to win {profit_text} profit. "
        f"Total return if it wins: {return_text}."
    )
