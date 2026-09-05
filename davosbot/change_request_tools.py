import re
import sqlite3
from contextlib import closing

from .config import BOT_DB_PATH
from .permissions import redact_secret


def _tool_change_request_risk(text: str) -> str:
    lower = (text or "").lower()
    if re.search(
        r"\b(permission|admin|password|private\s+(?:send|message|text)|send_imessage|"
        r"memory|soul|schema|migration|database|db\s+schema|tool\s+gate|owner[-\s]?only|"
        r"write_file|shell_exec|deploy|self[-\s]?edit|auto[-\s]?push|cron|reminder)\b",
        lower,
    ):
        return "RED"
    return "YELLOW"


def _tool_change_request_preview(text: str, max_chars: int = 1800) -> str:
    safe = redact_secret(text or "")
    safe = re.sub(r"\s+", " ", safe).strip()
    if len(safe) > max_chars:
        return safe[:max_chars].rstrip() + "..."
    return safe


def _log_change_request(request: str, reason: str = "", db_path: str = BOT_DB_PATH) -> str:
    safe_request = _tool_change_request_preview(request)
    safe_reason = _tool_change_request_preview(reason)
    risk = _tool_change_request_risk(f"{safe_request} {safe_reason}")
    summary = safe_request[:180].rstrip() or "Guarded Codex handoff requested"
    row_request = f"[TOOL-HANDOFF {risk}] {summary}"
    row_reason = "\n".join([
        "type=tool_change_request",
        f"risk={risk}",
        "status=review_only",
        "source=gemini_tool_log_change_request",
        f"request_text={safe_request}",
        f"reason_text={safe_reason or 'not_provided'}",
        "expected_bot_behavior=turn large/setup/repair requests into durable Codex handoffs instead of giving dismissive size replies or silently dropping intent",
        "safe_auto_fix_pipeline=Codex only: create a codex/... branch/worktree, patch, test, push, wait for CI, then Mini deploy/smoke; Davos must not edit production directly.",
        "blocked_actions=no live self-edit, no deploy, no shell/file/DB mutation outside change_log",
    ])
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.execute("INSERT INTO change_log (request, reason) VALUES (?, ?)", (row_request, row_reason))
        row_id = cur.lastrowid
        conn.commit()
    return (
        f"Logged guarded Codex handoff #{row_id} [{risk}]. "
        "I did not edit code or deploy. Text `ship safe cleanup` for the board."
    )
