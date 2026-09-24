"""Sender-scoped food planning with optional, explicitly confirmed checkout."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from .config import normalize_handle
from .food_checkout import begin_order, handle_checkout_control


@dataclass
class FoodDraft:
    request: str
    updated_at: float
    service: str = ""
    fulfillment: str = ""
    area: str = ""


_drafts: dict[str, FoodDraft] = {}
_lock = threading.Lock()
_TTL_SECONDS = 20 * 60
_START = re.compile(
    r"^(?:(?:hey|please|pls|davos|can you|could you)\s+)*"
    r"(?:order\s+(?:me\s+)?|(?:get|find)\s+me\s+(?:some\s+)?|i\s+(?:want|need)\s+(?:some\s+)?)"
    r"(?P<request>.*\b(?:wings?|pizza|sushi|tacos?|burgers?|food|dinner|lunch|breakfast|takeout)\b.*)$",
    re.IGNORECASE,
)
_OTHER_ACTION = re.compile(
    r"^(?:remind|remember|forget|send|text|dm|grant|revoke|fix|debug|repair|log|"
    r"schedule|cancel\s+(?!food)|what|when|why|how|who|show|list|tell|create|generate|"
    r"weather|forecast|news|stocks?|markets?|quotes?|scores?|search|look\s+up)\b",
    re.IGNORECASE,
)


def _service(text: str) -> str:
    if re.search(r"\bdoor\s*dash\b", text, re.I) and re.search(r"\buber\s*eats\b", text, re.I):
        return "compare services"
    if re.search(r"\bdoor\s*dash\b", text, re.I):
        return "DoorDash"
    if re.search(r"\buber\s*eats\b", text, re.I):
        return "Uber Eats"
    if re.search(r"\b(?:restaurant|direct|their\s+(?:site|website))\b", text, re.I):
        return "restaurant website"
    if re.fullmatch(r"(?:either|any|whatever|cheapest|compare|no preference)[.!]?", text, re.I):
        return "compare services"
    return ""


def _plan_food(sender: str, text: str, *, now: float | None = None):
    """Gather missing choices without holding the draft lock during browser IO.

    Called only after existing commands have had their normal chance to route.
    Each sender has a separate draft; unrelated task verbs never become slots.
    """
    clean = re.sub(r"\s+", " ", text or "").strip()
    key = normalize_handle(sender)
    timestamp = time.monotonic() if now is None else now
    if not key or not clean or len(clean) > 600:
        return None
    with _lock:
        for old_key in list(_drafts):
            if timestamp - _drafts[old_key].updated_at > _TTL_SECONDS:
                del _drafts[old_key]
        draft = _drafts.get(key)
        if draft and re.fullmatch(r"(?:cancel(?:\s+(?:food|the food order|food order))?|food cancel|never\s*mind|stop)[.!]?", clean, re.I):
            del _drafts[key]
            return "Food draft cleared. No order was placed."
        start = _START.fullmatch(clean)
        if start:
            draft = FoodDraft(start.group("request"), timestamp)
            _drafts[key] = draft
        elif draft is None or _OTHER_ACTION.match(clean):
            return None
        service = _service(clean)
        mode = re.search(r"\b(delivery|pickup|pick\s+up|takeout)\b", clean, re.I)
        area = re.search(r"\b(?:in|near|area:|zip:)\s+(.{2,70}?)(?:[.!?]|$)", clean, re.I)
        bare_area = bool(
            not start and draft.service and draft.fulfillment and not service and not mode
            and re.fullmatch(r"[\w\s,.'-]{2,70}", clean)
            and not re.fullmatch(r"[A-Z]{1,5}", clean)
            and clean.lower() not in {"yes", "no", "ok", "okay", "sure", "hello", "thanks"}
        )
        if not start and not (service or mode or area or bare_area):
            return None
        if service:
            draft.service = service
        if mode:
            draft.fulfillment = "delivery" if mode.group(1).lower() == "delivery" else "pickup"
        if area:
            draft.area = area.group(1).strip()
        elif bare_area:
            draft.area = clean
        draft.updated_at = timestamp
        if not draft.service:
            return "I can help find food and ordering links. DoorDash, Uber Eats, or the restaurant's own website? You can also say compare."
        if not draft.fulfillment:
            return f"{draft.service.capitalize() if draft.service[0].islower() else draft.service}. Delivery or pickup?"
        if not draft.area:
            return "What city, neighborhood, or ZIP should I search? No payment details needed."
        query = f"{draft.request} {draft.fulfillment} {draft.area} {draft.service} menu order"
        link = "https://www.google.com/search?" + urlencode({"q": query})
        del _drafts[key]
        handoff = (
            f"Food plan: {draft.request}\n{draft.service} · {draft.fulfillment} · {draft.area}\n"
            f"Find menus and ordering pages: {link}\n"
            "Checkout isn't connected to Davos yet, so nothing has been ordered. "
            "Open a restaurant's checkout to confirm the items, total, and place the order."
        )
        return draft, handoff


def handle_food_order(sender: str, text: str, *, now: float | None = None) -> str | None:
    clean = re.sub(r"\s+", " ", text or "").strip()
    cancelled_draft = None
    explicit_cancel = re.fullmatch(r"(?:cancel\s+(?:food|food order|the food order)|food cancel)[.!]?", clean, re.I)
    with _lock:
        key = normalize_handle(sender)
        draft_cancel = key in _drafts and re.fullmatch(r"(?:cancel|never\s*mind|stop)[.!]?", clean, re.I)
        if explicit_cancel or draft_cancel:
            cancelled_draft = _drafts.pop(key, None)
    if explicit_cancel or draft_cancel:
        clean = "food cancel"
    try:
        control = handle_checkout_control(sender, clean)
    except Exception:
        return "Food checkout status is unavailable. Check the merchant's order history before starting another purchase."
    if cancelled_draft:
        # A new planning draft can coexist with an older unresolved attempt.
        # Clearing the plan cannot turn that older purchase into a known failure.
        return "Food draft cleared. " + (control or "No order was placed.")
    if control is not None:
        return control
    result = _plan_food(sender, clean, now=now)
    if not isinstance(result, tuple):
        return result
    draft, handoff = result
    try:
        checkout = begin_order(sender, draft.service, {
            "goal": draft.request, "fulfillment": draft.fulfillment,
            "area": draft.area, "details": "",
        })
    except Exception:
        # A ledger/browser failure must not become a claim that checkout worked.
        return "Food checkout couldn't be verified. Check food status before trying again; I won't assume an order failed or repeat a purchase."
    if checkout is None:
        return handoff
    if "ordering link below" in checkout:
        link = "https://www.google.com/search?" + urlencode({"q": f"{draft.request} {draft.fulfillment} {draft.area} {draft.service} menu order"})
        return checkout + "\n" + link
    return checkout
