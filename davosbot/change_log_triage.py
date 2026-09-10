"""Pure change-log risk grouping, without loading bot runtime state."""

import re


def _classify_change_request(text: str) -> str:
    """Deterministic backlog risk color. This is triage, not authorization."""
    lower = (text or "").lower()
    explicit_color = re.match(r"^\s*\[[^\]]*\b(green|yellow|red)\b[^\]]*\]", lower)
    if explicit_color:
        return explicit_color.group(1)
    red_patterns = [
        r"\b(?:p0|priority\s*0|code\s*red|red\s+log|log\s+red|mark\s+(?:it\s+)?red)\b",
        r"\bgithub\s+pat\b", r"\bgemini\s+(?:api\s+)?key\b", r"\bapi\s*key\b", r"\bsecret\b", r"\btoken\b",
        r"\bpermissions?\.py\b", r"\bpermissions?\b", r"\badmin\b", r"\badmin_password\b", r"\bpassword\b",
        r"\bmemory\.md\b", r"\bsoul\.md\b", r"\bmemory mutation\b",
        r"\bprivate\s+(?:message|send|text|imessage)\b", r"\b1\s*on\s*1\b", r"\b1on1\b",
        r"\bdm\s+(?:send|text|message)\b", r"\bdirect message\b", r"\boutbound\b", r"\bsend routing\b",
        r"\breminders?\b", r"\bcron execution\b", r"\bdb schema\b", r"\bmigration\b",
        r"\btool permission\b", r"\bself[- ]?(?:edit|deploy)\b", r"\bmodel routing\b.*\btool\b",
    ]
    yellow_patterns = [
        r"\bpersona\b", r"\bcron\b", r"\bjobs?\b", r"\bimage\b", r"\bgpt\b", r"\bgemini\b",
        r"\bmodel\b", r"\bimessage\b", r"@davos", r"\bmention\b", r"\bweather\b", r"\blocation\b",
        r"\bcopilot\b", r"\bgithub\b", r"\bworkflow\b", r"\bactions?\b",
    ]
    green_patterns = [
        r"\bdocs?\b", r"\breadme\b", r"\bhelp text\b", r"\bwording\b", r"\btone\b", r"\bprompt\b",
        r"\btests?\b", r"\bcleanup\b", r"\bformat\b", r"\btypo\b", r"\bdependency\b",
        r"\bsports bias\b", r"\bhomer\b",
    ]
    for pattern in red_patterns:
        if re.search(pattern, lower):
            return "red"
    for pattern in yellow_patterns:
        if re.search(pattern, lower):
            return "yellow"
    for pattern in green_patterns:
        if re.search(pattern, lower):
            return "green"
    return "yellow"


def _change_log_row_parts(row) -> tuple[int, str, str, str]:
    row_id, request, reason, created_ts = row
    return int(row_id), request or "", reason or "", created_ts or ""


def _bucket_change_log_rows(rows) -> dict[str, list[tuple[int, str, str, str]]]:
    buckets = {"green": [], "yellow": [], "red": []}
    for row in rows:
        row_id, request, reason, created_ts = _change_log_row_parts(row)
        color = _classify_change_request(request)
        buckets[color].append((row_id, request, reason, created_ts))
    return buckets
