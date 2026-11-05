import logging
import re
from typing import Any

import requests

from .config import OWNER_ALERT_WEBHOOK_TIMEOUT, OWNER_ALERT_WEBHOOK_URL
from .permissions import redact_secret

logger = logging.getLogger(__name__)


def _redact_alert_text(text: str) -> str:
    redacted = redact_secret(text)
    return re.sub(
        r"\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*['\"]?[^,\s)]+",
        r"\1=[redacted]",
        redacted,
        flags=re.IGNORECASE,
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_alert_text(value)
    if isinstance(value, dict):
        return {str(k): _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return [_redact_value(v) for v in value]
    return value


def _build_generic_payload(event_type: str, message: str, metadata: dict | None = None) -> dict[str, Any]:
    return {
        "source": "davosbot",
        "event_type": _redact_alert_text(str(event_type or "owner_alert"))[:120],
        "message": _redact_alert_text(str(message or ""))[:2000],
        "metadata": _redact_value(metadata or {}),
    }


def _alert_text(payload: dict[str, Any]) -> str:
    lines = [
        f"DavosBot alert: {payload['event_type']}",
        str(payload["message"]),
    ]
    metadata = payload.get("metadata") or {}
    if metadata:
        safe_pairs = []
        for key, value in metadata.items():
            safe_pairs.append(f"{key}={value}")
        lines.append("Metadata: " + ", ".join(safe_pairs))
    return "\n".join(line for line in lines if line).strip()


def _webhook_kind(url: str) -> str:
    clean = url.lower()
    if "discord.com/api/webhooks/" in clean or "discordapp.com/api/webhooks/" in clean:
        return "discord"
    if "hooks.slack.com/" in clean:
        return "slack"
    if "ntfy.sh/" in clean:
        return "ntfy"
    return "generic"


def _request_kwargs(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = _alert_text(payload)
    kind = _webhook_kind(url)
    if kind == "discord":
        return {"json": {"content": text[:2000]}}
    if kind == "slack":
        return {"json": {"text": text[:3000]}}
    if kind == "ntfy":
        return {
            "data": text.encode("utf-8"),
            "headers": {
                "Title": "DavosBot alert",
                "Tags": "warning",
            },
        }
    return {"json": payload}


def send_owner_alert(event_type: str, message: str, metadata: dict | None = None) -> bool:
    """Send a redacted best-effort owner alert without relying on iMessage."""
    url = OWNER_ALERT_WEBHOOK_URL
    if not url:
        return False

    payload = _build_generic_payload(event_type, message, metadata)

    try:
        resp = requests.post(url, timeout=OWNER_ALERT_WEBHOOK_TIMEOUT, **_request_kwargs(url, payload))
        if resp.status_code >= 400:
            logger.warning("Owner alert webhook returned HTTP %s", resp.status_code)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Owner alert webhook failed: %s", type(exc).__name__)
        return False
