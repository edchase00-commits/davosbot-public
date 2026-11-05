"""Durable, actor-bound approval boundary for optional merchant checkout.

Browser preparation may change a cart but cannot buy. Only the callback passed
to submit can reserve a single final purchase attempt. An ambiguous attempt is
never replayed. State lives beside dedicated browser profiles, outside git.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .config import normalize_handle
from .permissions import is_admin

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_ACTIVE = ("preparing", "needs_login", "needs_input", "quoted", "submitting", "unknown")
_SERVICES = {"doordash": "doordash", "uber eats": "ubereats", "ubereats": "ubereats"}
_CONTROL = re.compile(r"^food\s+(status|resume|cancel|confirm)(?:\s+([a-z0-9]+))?[.!]?$", re.I)
_DETAILS = re.compile(r"^food\s+details\s+(.+)$", re.I)
_SECRET = re.compile(r"\b(?:password|passcode|cvv|cvc|card\s*(?:number|no)|otp)\b|(?:\d[ -]?){13,19}", re.I)
_UNSET = object()


def _lock(actor):
    with _locks_guard:
        return _locks.setdefault(actor, threading.RLock())


def _adapter():
    return importlib.import_module(".checkout_browser", __package__)


def _storage_path():
    return Path.home() / ".davosbot" / "checkout" / "state.sqlite3"


def _actor_key(actor):
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()


def _profile_id(actor, service):
    return hashlib.sha256((actor + "\0" + service).encode("utf-8")).hexdigest()


def _url_allowed(url, service):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        base = {"doordash": "doordash.com", "ubereats": "ubereats.com"}[service]
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and (host == base or host.endswith("." + base)))
    except (KeyError, ValueError):
        return False


def _text(value, limit=300):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def validate_quote(quote, actor, service, now):
    """Independent shape/account/expiry check before showing or approving a cart."""
    if not isinstance(quote, dict) or quote.get("version") != 1:
        return False
    if quote.get("service") != service or quote.get("profile_id") != _profile_id(actor, service):
        return False
    if not all(_text(quote.get(field), 350 if field == "address" else 300)
               for field in ("quote_id", "merchant", "payment_label", "address")):
        return False
    review = quote.get("review_text")
    if (not isinstance(review, str) or not 1 <= len(review) <= 3500
            or any(ord(c) < 32 and c not in "\n\t" for c in review)
            or re.search(r"(?:\d[ -]?){13,19}", review)):
        return False
    # An adapter may expose only a payment label, never a full card number.
    if re.search(r"(?:\d[ -]?){7,}", quote["payment_label"]):
        return False
    if quote.get("fulfillment") not in ("pickup", "delivery"):
        return False
    if quote.get("currency") not in ("USD", "CAD", "AUD", "EUR", "GBP"):
        return False
    if type(quote.get("total_minor")) is not int or not 0 < quote["total_minor"] <= 1_000_000:
        return False
    if not _url_allowed(quote.get("checkout_url"), service):
        return False
    if not isinstance(quote.get("evidence_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", quote["evidence_hash"]):
        return False
    observed, expires = quote.get("observed_at"), quote.get("expires_at")
    if (type(observed) not in (int, float) or type(expires) not in (int, float)
            or not observed <= now < expires <= observed + 300 or now - observed > 300):
        return False
    items = quote.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        return False
    for item in items:
        if not isinstance(item, dict) or not _text(item.get("name")):
            return False
        if type(item.get("quantity")) is not int or not 1 <= item["quantity"] <= 100:
            return False
        options = item.get("options", [])
        if not isinstance(options, list) or len(options) > 30 or not all(_text(v) for v in options):
            return False
    return True


class CheckoutStore:
    """Small separate ledger. Conditional writes coordinate processes and channels."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else _storage_path()

    def _connect(self, create=True):
        if not create and not self.path.exists():
            return None
        # The managed directory and file must not redirect state to another user
        # or checkout. System HOME ancestors may legitimately be OS symlinks.
        for managed in (self.path.parent.parent, self.path.parent, self.path):
            if managed.is_symlink():
                raise ValueError("Checkout state path is a symlink")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        if os.name != "nt":
            self.path.chmod(0o600)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, service TEXT NOT NULL,
            state TEXT NOT NULL, request TEXT NOT NULL, quote TEXT,
            token TEXT, reference TEXT, receipt TEXT, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_actor ON orders(actor)
            WHERE state IN ('preparing','needs_login','needs_input','quoted','submitting','unknown')""")
        conn.commit()
        return conn

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for field in ("request", "quote", "reference", "receipt"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def latest(self, actor):
        conn = self._connect(create=False)
        if conn is None:
            return None
        try:
            return self._decode(conn.execute(
                "SELECT * FROM orders WHERE actor=? ORDER BY CASE WHEN state IN (?,?,?,?,?,?) "
                "THEN 0 ELSE 1 END, rowid DESC LIMIT 1", (_actor_key(actor), *_ACTIVE)).fetchone())
        finally:
            conn.close()

    def begin(self, actor, service, request, now):
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM orders WHERE actor=? AND state IN (?,?,?,?,?,?)",
                                   (_actor_key(actor), *_ACTIVE)).fetchone()
                if row:
                    return self._decode(row), False
                order_id = secrets.token_hex(12)
                conn.execute("INSERT INTO orders(id,actor,service,state,request,updated_at) VALUES(?,?,?,'preparing',?,?)",
                             (order_id, _actor_key(actor), service, json.dumps(request), now))
                row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                return self._decode(row), True
        finally:
            conn.close()

    def transition(self, actor, order_id, states, state, now, *, expected_token=_UNSET, **fields):
        allowed = {"quote", "token", "reference", "receipt", "request"}
        if set(fields) - allowed or state not in (*_ACTIVE, "confirmed", "cancelled", "failed"):
            raise ValueError("Invalid checkout transition")
        conn = self._connect()
        values = [state, now]
        setters = ["state=?", "updated_at=?"]
        for field, value in fields.items():
            setters.append(field + "=?")
            values.append(json.dumps(value) if field != "token" and value is not None else value)
        values.extend((order_id, _actor_key(actor), *states))
        token_clause = ""
        if expected_token is not _UNSET:
            token_clause = " AND token=?"
            values.append(expected_token)
        try:
            with conn:
                changed = conn.execute(
                    "UPDATE orders SET " + ",".join(setters) + " WHERE id=? AND actor=? AND state IN ("
                    + ",".join("?" for _ in states) + ")" + token_clause, values).rowcount
                return changed == 1
        finally:
            conn.close()


def _quote_reply(row, now):
    quote = row["quote"]
    # The model nominates indexes for checks, but cannot summarize away an extra
    # paid item. Always show the complete bounded merchant checkout text.
    return (f"Ready for your review: {quote['merchant']}\n"
            f"Checkout text from the merchant:\n{quote['review_text']}\n\n"
            f"{quote['fulfillment'].capitalize()}: {quote['address']}\n"
            f"Total: {quote['currency']} {quote['total_minor'] / 100:.2f}, including the displayed fees/tip.\n"
            f"Payment: {quote['payment_label']}\n"
            f"Text food confirm {row['token']} within {max(0, int(quote['expires_at'] - now))} seconds to place this exact order, "
            "or food cancel. I will recheck the cart and total before submitting. Nothing has been ordered yet.")


def _summary(row, now):
    if row is None:
        return "No food checkout is open. Tell me what you want to eat to start."
    state = row["state"]
    if state == "confirmed":
        receipt = row["receipt"]
        return f"Merchant confirmed order {receipt['order_id']}: {receipt['status']}.\n{receipt['url']}"
    if state in ("submitting", "unknown"):
        return ("The purchase attempt is unresolved. It may have gone through, so I won't submit it again. "
                "Text food status to check for a verified receipt, or inspect the merchant's order history.")
    if state == "quoted":
        if row["quote"]["expires_at"] <= now:
            return "The food quote expired. Text food resume for a fresh cart and total; nothing has been ordered."
        return _quote_reply(row, now)
    if state == "needs_login":
        return ("Sign in to your own dedicated ordering browser on the Mini, then text food resume. "
                "Enter passwords, MFA codes, addresses and payment details directly on the merchant site. "
                "Nothing has been ordered.")
    if state == "needs_input":
        return "The cart needs your input. Reply food details followed by your choices, or food resume after checking the browser. Nothing has been ordered."
    if state == "preparing":
        return ("Food preparation is running or was interrupted. Nothing has been submitted. "
                "If it stays here, use food cancel and check your cart before starting again.")
    if state == "cancelled":
        return "Food draft cleared. No purchase was submitted by this checkout. Your merchant cart may still contain items."
    return "Checkout could not continue. No purchase was submitted. Tell me what you want to eat to try a new plan."


def _prepare(actor, row, store, adapter, now):
    try:
        result = adapter.prepare(actor, row["service"], dict(row["request"]))
    except Exception:
        # No raw browser/model output in logs or replies; it may include account data.
        store.transition(actor, row["id"], ("preparing",), "needs_input", now())
        return "Cart preparation stopped before purchase. Check your dedicated browser, then text food resume. Nothing has been ordered."
    status = result.get("status") if isinstance(result, dict) else None
    if status == "ready" and validate_quote(result.get("quote"), actor, row["service"], now()):
        quote = result["quote"]
        changed = store.transition(actor, row["id"], ("preparing",), "quoted", now(),
                                   quote=quote, token=secrets.token_hex(4), reference=quote)
        return _summary(store.latest(actor), now()) if changed else "The food draft changed while preparing. Check food status."
    next_state = status if status in ("needs_login", "needs_input") else "failed"
    store.transition(actor, row["id"], ("preparing",), next_state, now())
    if status == "unavailable":
        return ("Automatic checkout isn't ready for this account/service. Nothing has been ordered. "
                "The Mini needs the optional checkout browser and your own signed-in profile. "
                "You can still use the ordering link below.")
    question = result.get("question", "") if isinstance(result, dict) else ""
    # Questions are untrusted provider output, bounded and barred from requesting secrets.
    if next_state == "needs_input" and _text(question, 500) and not _SECRET.search(question):
        return question + " Reply food details followed by your choices. Nothing has been ordered."
    return _summary(store.latest(actor), now())


def begin_order(sender, service, request, *, store=None, adapter=None, clock=time.time):
    """Return None only when the optional implementation is absent (link fallback)."""
    actor = normalize_handle(sender)
    canonical_service = _SERVICES.get(service.lower())
    if not actor or not is_admin(actor):
        return "Food checkout requires active owner or admin access."
    if not canonical_service:
        return None
    if (not isinstance(request, dict) or set(request) - {"goal", "fulfillment", "area", "details"}
            or not all(isinstance(value, str) and len(value) <= 2400 for value in request.values())
            or not all(request.get(field) for field in ("goal", "fulfillment", "area"))):
        return "I need the food request, pickup or delivery, and an area before preparing checkout."
    if any(_SECRET.search(value) for value in request.values()):
        return "Enter account and payment details directly on the merchant site, not in chat."
    try:
        adapter = adapter or _adapter()
    except ImportError:
        return None
    store = store or CheckoutStore()
    with _lock(actor):
        row, created = store.begin(actor, canonical_service, request, clock())
        return _prepare(actor, row, store, adapter, clock) if created else _summary(row, clock())


def _valid_receipt(receipt, quote, now):
    return (isinstance(receipt, dict) and _text(receipt.get("order_id"), 160)
            and receipt.get("status") in ("placed", "accepted", "confirmed") and _url_allowed(receipt.get("url"), quote["service"])
            and receipt.get("merchant") == quote["merchant"]
            and receipt.get("currency") == quote["currency"]
            and type(receipt.get("total_minor")) is int and receipt["total_minor"] == quote["total_minor"]
            and receipt.get("profile_id") == quote["profile_id"] and receipt.get("quote_id") == quote["quote_id"]
            and receipt.get("evidence") == "merchant_dom"
            and type(receipt.get("observed_at")) in (int, float)
            and quote["observed_at"] <= receipt["observed_at"] <= now)


def _finish(actor, row, result, store, now):
    # This path is used only after the purchase reservation. Any malformed or
    # negative result still means unknown, not permission for another attempt.
    result = result if isinstance(result, dict) else {}
    receipt = result.get("receipt")
    reference = dict(row["reference"] or row["quote"])
    confirmed = (result.get("status") == "confirmed" and _valid_receipt(receipt, row["quote"], now())
                 and (not reference.get("order_id") or receipt["order_id"] == reference["order_id"]))
    candidate = result.get("reference")
    if (isinstance(candidate, dict) and _text(candidate.get("order_id"), 160)
            and _url_allowed(candidate.get("receipt_url"), row["service"])
            and all(candidate.get(field) == row["quote"][field] for field in ("service", "profile_id", "quote_id"))
            and (not reference.get("order_id") or candidate["order_id"] == reference["order_id"])):
        reference.update(order_id=candidate["order_id"], receipt_url=candidate["receipt_url"])
    store.transition(actor, row["id"], ("submitting", "unknown"), "confirmed" if confirmed else "unknown", now(),
                     receipt=receipt if confirmed else None, reference=reference)
    return _summary(store.latest(actor), now())


def handle_checkout_control(sender, text, *, store=None, adapter=None, clock=time.time):
    clean = re.sub(r"\s+", " ", text or "").strip()
    match, details = _CONTROL.fullmatch(clean), _DETAILS.fullmatch(clean)
    if not match and not details:
        return None
    actor = normalize_handle(sender)
    if not actor or not is_admin(actor):
        return "Food checkout requires active owner or admin access."
    if _SECRET.search(clean):
        return "Enter account and payment details directly on the merchant site, not in chat."
    action = "resume" if details else match.group(1).lower()
    token = "" if details else (match.group(2) or "").lower()
    if token and action != "confirm":
        return "Use food status, food resume, food cancel, or food confirm followed by the quote's code."
    store = store or CheckoutStore()
    with _lock(actor):
        row = store.latest(actor)
        if row is None:
            return _summary(None, clock())
        if action == "cancel":
            if row["state"] in ("submitting", "unknown", "confirmed"):
                return "I can't cancel a submitted or unresolved purchase here. Check the merchant's order history; I won't retry it."
            store.transition(actor, row["id"], ("preparing", "needs_login", "needs_input", "quoted"), "cancelled", clock(), token=None)
            return _summary(store.latest(actor), clock())
        try:
            adapter = adapter or _adapter()
        except ImportError:
            return _summary(row, clock())
        if action == "status":
            if row["state"] in ("submitting", "unknown"):
                try:
                    result = adapter.inspect_order(actor, row["reference"])
                except Exception:
                    result = {"status": "unknown"}
                return _finish(actor, row, result, store, clock)
            return _summary(row, clock())
        if action == "resume":
            if row["state"] not in ("needs_input", "needs_login", "quoted"):
                return _summary(row, clock())
            request = dict(row["request"])
            if details:
                new_details = details.group(1)
                if len(new_details) > 600 or len(request.get("details", "")) + len(new_details) > 2400:
                    return "Keep food choices under 600 characters; start a fresh plan if the request has changed."
                request["details"] = (request.get("details", "") + "\n" + new_details).strip()
            if not store.transition(actor, row["id"], (row["state"],), "preparing", clock(), request=request, token=None, quote=None):
                return _summary(store.latest(actor), clock())
            return _prepare(actor, store.latest(actor), store, adapter, clock)
        if row["state"] != "quoted":
            return _summary(row, clock())
        if not token or not secrets.compare_digest(token, row["token"] or ""):
            return "Use the exact food confirm code shown with your cart and total. Nothing has been submitted."
        if not validate_quote(row["quote"], actor, row["service"], clock()):
            return "That quote expired or is invalid. Text food resume for a fresh review. Nothing has been submitted."

        reserved = False

        def reserve():
            # Final authorization and durable CAS are checked again immediately
            # before the browser's final click, including revocations mid-request.
            nonlocal reserved
            if reserved:
                return False
            reserved = (is_admin(actor) and validate_quote(row["quote"], actor, row["service"], clock())
                        and store.transition(actor, row["id"], ("quoted",), "submitting", clock(),
                                             expected_token=row["token"], token=None))
            return reserved

        try:
            result = adapter.submit(actor, row["quote"], on_submit=reserve)
        except Exception:
            result = {"status": "unknown"}
        latest = store.latest(actor)
        if reserved and latest["state"] in ("submitting", "unknown"):
            return _finish(actor, row, result, store, clock)
        if latest["state"] != "quoted" or latest["token"] != row["token"]:
            return _summary(latest, clock())
        # Never accept a receipt from an adapter that did not reserve the attempt.
        store.transition(actor, row["id"], ("quoted",), "needs_input", clock(), expected_token=row["token"], token=None, quote=None)
        return "Checkout did not submit. The cart, account, price or access may have changed. Text food resume for a fresh review."
