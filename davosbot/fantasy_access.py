"""Signed DavosBot client for Fourth Down access management."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import FANTASY_ACCESS_PRIVATE_KEY_PATH, FANTASY_DASHBOARD_URL

_CONTROL_PATH = "/api/access/control"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]{1,190}\.[^@\s]{2,63}$")
_ROLES = {"viewer", "editor", "owner"}
_USER_AGENT = "DavosBot/1.0"
_READ_ONLY_ACTIONS = {"list", "status"}
_READ_RETRY_DELAYS_SECONDS = (0.35, 0.9)
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class FantasyAccessError(RuntimeError):
    """Safe user-facing failure from the dashboard access service."""


def request_access(handle: str, email: str, chat_id: str) -> dict[str, Any]:
    normalized_email = email.strip().lower()
    if not _EMAIL_RE.fullmatch(normalized_email):
        raise FantasyAccessError(
            "That email does not look valid. Try: @Davos fantasy request name@example.com"
        )
    return _call_control(
        {
            "action": "request",
            "handle": _clean_handle(handle),
            "email": normalized_email,
            "chatId": str(chat_id)[:160],
        }
    )


def get_access_status(handle: str) -> dict[str, Any]:
    return _call_control({"action": "status", "handle": _clean_handle(handle)})


def list_access(*, pending_only: bool = False) -> dict[str, Any]:
    return _call_control(
        {"action": "list", "status": "pending" if pending_only else "all"}
    )


def grant_access(request_id: int, role: str) -> dict[str, Any]:
    return _call_control(
        {
            "action": "grant",
            "memberId": _positive_id(request_id),
            "role": _role(role),
        }
    )


def set_access_role(member_id: int, role: str) -> dict[str, Any]:
    return _call_control(
        {
            "action": "set_role",
            "memberId": _positive_id(member_id),
            "role": _role(role),
        }
    )


def revoke_access(member_id: int) -> dict[str, Any]:
    return _call_control(
        {"action": "revoke", "memberId": _positive_id(member_id)}
    )


def _call_control(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = _validated_base_url(FANTASY_DASHBOARD_URL)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    retry_delays = (
        _READ_RETRY_DELAYS_SECONDS
        if payload.get("action") in _READ_ONLY_ACTIONS
        else ()
    )

    for attempt in range(len(retry_delays) + 1):
        request = _signed_request(base_url, body)
        try:
            with urlopen(request, timeout=8) as response:
                raw = response.read(32768)
            break
        except HTTPError as exc:
            try:
                raw = exc.read(32768)
            finally:
                exc.close()
            if exc.code in _TRANSIENT_HTTP_STATUSES:
                if attempt < len(retry_delays):
                    time.sleep(retry_delays[attempt])
                    continue
                raise FantasyAccessError(
                    "Fourth Down access is temporarily unavailable. Try again in a minute."
                ) from None
            message = _response_error(raw)
            raise FantasyAccessError(
                message or "Fourth Down rejected that request."
            ) from None
        except (URLError, TimeoutError, OSError):
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise FantasyAccessError(
                "Fourth Down access is temporarily unavailable. Try again in a minute."
            ) from None

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FantasyAccessError("Fourth Down returned an invalid response.") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise FantasyAccessError("Fourth Down could not complete that access request.")
    return result


def _signed_request(base_url: str, body: bytes) -> Request:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{nonce}\nPOST\n{_CONTROL_PATH}\n{body_hash}"
    signature = _sign(canonical, FANTASY_ACCESS_PRIVATE_KEY_PATH)

    return Request(
        f"{base_url}{_CONTROL_PATH}",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": _USER_AGENT,
            "x-davos-timestamp": timestamp,
            "x-davos-nonce": nonce,
            "x-davos-signature": signature,
        },
    )


def _sign(canonical: str, key_path: Path) -> str:
    try:
        if not key_path.is_file():
            raise FantasyAccessError(
                "Fantasy access control is not configured on this Davos host."
            )
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
            input=canonical.encode("utf-8"),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except FantasyAccessError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise FantasyAccessError(
            "Fantasy access signing is unavailable on this Davos host."
        ) from None
    if result.returncode != 0 or not result.stdout:
        raise FantasyAccessError(
            "Fantasy access signing is unavailable on this Davos host."
        )
    return base64.b64encode(result.stdout).decode("ascii")


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise FantasyAccessError("The fantasy dashboard URL is not configured safely.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _response_error(raw: bytes) -> str:
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(result, dict):
        return ""
    message = str(result.get("error", "")).strip()
    return message[:240]


def _positive_id(value: int) -> int:
    number = int(value)
    if number < 1:
        raise FantasyAccessError("Access request IDs must be positive numbers.")
    return number


def _role(value: str) -> str:
    role = value.strip().lower()
    if role not in _ROLES:
        raise FantasyAccessError("Role must be viewer, editor, or owner.")
    return role


def _clean_handle(value: str) -> str:
    handle = value.strip().lower()
    if not handle or len(handle) > 160:
        raise FantasyAccessError("The requesting iMessage handle is invalid.")
    return handle
