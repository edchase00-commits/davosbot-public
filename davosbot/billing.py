import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from .runtime_locks import BUDGET_ALERT_LOCK

from .config import (
    BOT_DB_PATH,
    GEMINI_BUDGET_ALERT_COOLDOWN_MINUTES,
    GEMINI_DAILY_ALERT_USD,
    GEMINI_DAILY_BUDGET_USD,
    GEMINI_ENABLED,
)

logger = logging.getLogger(__name__)

# Shared Gemini estimate per 1M tokens.
# This is intentionally conservative/shared until usage rows store per-model rates.
GEMINI_INPUT_RATE_USD = 0.30 / 1_000_000
GEMINI_OUTPUT_RATE_USD = 2.50 / 1_000_000

_LAST_GEMINI_BUDGET_ALERT_AT = 0.0


@dataclass(frozen=True)
class GeminiUsageSummary:
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True)
class GeminiBudgetDecision:
    allowed: bool
    reason: str = ""
    estimated_today_usd: float = 0.0


def estimate_gemini_cost(prompt_tokens: int | None, candidates_tokens: int | None) -> float:
    prompt = int(prompt_tokens or 0)
    candidates = int(candidates_tokens or 0)
    return prompt * GEMINI_INPUT_RATE_USD + candidates * GEMINI_OUTPUT_RATE_USD


def log_gemini_usage(prompt_tokens: int, candidates_tokens: int, total_tokens: int, source: str) -> None:
    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO gemini_usage (prompt_tokens, candidates_tokens, total_tokens, source) VALUES (?,?,?,?)",
                (prompt_tokens, candidates_tokens, total_tokens, source),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to log Gemini usage from %s: %s", source, exc)


def get_gemini_usage_summary(period: str = "today") -> GeminiUsageSummary:
    where = ""
    if period == "today":
        where = "WHERE date(timestamp) = date('now')"
    elif period == "month":
        where = "WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')"
    elif period == "all":
        where = ""
    else:
        raise ValueError(f"Unsupported Gemini usage period: {period}")

    try:
        with closing(sqlite3.connect(BOT_DB_PATH)) as conn:
            row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(candidates_tokens), 0),
                    COALESCE(SUM(total_tokens), 0),
                    COUNT(*)
                FROM gemini_usage
                {where}
                """
            ).fetchone()
    except Exception as exc:
        logger.warning("Failed to read Gemini usage summary: %s", exc)
        return GeminiUsageSummary()

    prompt, candidates, total, calls = row or (0, 0, 0, 0)
    return GeminiUsageSummary(
        prompt_tokens=int(prompt or 0),
        candidates_tokens=int(candidates or 0),
        total_tokens=int(total or 0),
        calls=int(calls or 0),
        estimated_cost_usd=estimate_gemini_cost(int(prompt or 0), int(candidates or 0)),
    )


def _maybe_send_gemini_budget_alert(summary: GeminiUsageSummary, event_type: str, message: str) -> None:
    global _LAST_GEMINI_BUDGET_ALERT_AT
    cooldown_s = max(float(GEMINI_BUDGET_ALERT_COOLDOWN_MINUTES or 0), 1.0) * 60
    now = time.time()
    with BUDGET_ALERT_LOCK:
        if now - _LAST_GEMINI_BUDGET_ALERT_AT < cooldown_s:
            return
        _LAST_GEMINI_BUDGET_ALERT_AT = now
    try:
        from .alerts import send_owner_alert

        send_owner_alert(
            event_type,
            message,
            {
                "today_estimated_usd": round(summary.estimated_cost_usd, 6),
                "today_calls": summary.calls,
                "daily_alert_usd": GEMINI_DAILY_ALERT_USD,
                "daily_budget_usd": GEMINI_DAILY_BUDGET_USD,
            },
        )
    except Exception as exc:
        logger.warning("Gemini budget alert failed: %s", exc)


def check_gemini_budget(source: str = "gemini") -> GeminiBudgetDecision:
    if not GEMINI_ENABLED:
        return GeminiBudgetDecision(False, "Gemini is disabled by GEMINI_ENABLED=false.")

    summary = get_gemini_usage_summary("today")
    if GEMINI_DAILY_BUDGET_USD > 0 and summary.estimated_cost_usd >= GEMINI_DAILY_BUDGET_USD:
        message = (
            "Gemini daily budget reached; blocking Gemini calls until the budget is raised "
            "or the UTC day rolls over."
        )
        _maybe_send_gemini_budget_alert(summary, "gemini_budget_blocked", message)
        return GeminiBudgetDecision(False, message, summary.estimated_cost_usd)

    if GEMINI_DAILY_ALERT_USD > 0 and summary.estimated_cost_usd >= GEMINI_DAILY_ALERT_USD:
        _maybe_send_gemini_budget_alert(
            summary,
            "gemini_budget_warning",
            f"Gemini daily spend estimate crossed the alert threshold during {source}.",
        )

    return GeminiBudgetDecision(True, estimated_today_usd=summary.estimated_cost_usd)
