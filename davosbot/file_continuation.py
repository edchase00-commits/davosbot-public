"""Short-lived owner file-name clarifications, never authority from chat history."""

from dataclasses import dataclass
import os
import re
import time
from threading import RLock

from .config import PROJECT_ROOT, normalize_handle


_TTL = 300
_pending = {}
_lock = RLock()
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:csv|tsv|txt|json|md)"
_YES = re.compile(r"(?:yes|yep|yeah|ok(?:ay)?|sure)(?:[, ]+(?:please|do it|go ahead))?[.!]*|(?:do it|go ahead)[.!]*", re.I)
_START = re.compile(r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:save|export|write)\b", re.I)
_FORMAT = re.compile(r"\b(csv|tsv|txt|json|md)\b", re.I)
_DEFER_OR_META = re.compile(
    r"\b(?:do\s+not|don['’]t|not\s+yet|no\s+(?:write|writing|sav(?:e|ing))|"
    r"plan(?:ning)?|preview|dry[ -]?run|code|script|show|explain|instructions?|"
    r"how\s+to|examples?|demonstrate|pretend|roleplay|hypothetical|"
    r"later|tomorrow|tonight|wait|until|unless|after|before|when|once|if)\b",
    re.I,
)
_RESERVED = {"memory", "soul", "agents", "readme", "config", "package", "settings", "gc_state", "requirements"}
ASK_NAME = "What filename should I use, such as totals.csv? Nothing was written."
NO_WRITE = "I didn't run a file write. Nothing was saved by this follow-up. Please send the save request again."


@dataclass(frozen=True)
class Selection:
    request: str
    filename: str


def _key(sender, chat_id):
    chat = (chat_id or "").strip()
    return normalize_handle(sender or ""), chat.lower() if re.fullmatch(r"[0-9a-f]{32}", chat, re.I) else normalize_handle(chat)


def safe_filename(value):
    return isinstance(value, str) and bool(re.fullmatch(_NAME, value, re.I)) and value.rsplit(".", 1)[0].lower() not in _RESERVED


def _answer(text):
    raw = (text or "").strip()
    match = re.fullmatch(rf"(?:(?:use|call it|name it)\s+)?[`\"']?({_NAME})[`\"']?(?:\s+(?:please|for the filename))?[.!]?", raw, re.I)
    return match[1] if match and safe_filename(match[1]) else None


def initial_request(text):
    raw = (text or "").strip()
    header = re.split(r"[:\n]", raw, maxsplit=1)[0]
    # Intentionally limited to explicit data-file exports. No quotes, code,
    # destinations, or instructional/meta examples start a continuation.
    return bool(
        0 < len(raw) <= 12000 and _START.match(raw) and _FORMAT.search(raw)
        and not _DEFER_OR_META.search(header)
        and not re.search(r"\b(?:do\s+not|don['’]t|not\s+yet|no\s+(?:write|writing|sav(?:e|ing))|only\s+(?:after|if)|wait\s+(?:for|until))\b", raw, re.I)
        and re.search(r"\b(?:file|rows?|table|data|text|notes?)\b", raw, re.I)
        and not re.search(r"\b(?:pretend|roleplay|example of|hypothetical|explain|script|overwrite|replace|append|delete|remove|send|upload|private|password|secret)\b|[/\\~]|\.\.", header, re.I)
    )


def remember(sender, chat_id, request, reply, *, delivered, attempted_tools, has_image=False):
    """Called only from the verified owner's just-completed model turn."""
    from .tool_outcomes import READ_ONLY_TOOLS
    if not delivered or has_image or not initial_request(request):
        return False
    if any(name not in READ_ONLY_TOOLS for name in attempted_tools):
        return False
    question = (reply or "").strip()
    if len(question) > 400 or "?" not in question:
        return False
    # The answer must actually be a filename clarification, not arbitrary
    # model text ending in a question after a claimed completion.
    generic = re.fullmatch(r"(?:What|Which)\s+(?:file\s*name|name)(?:\s+should I (?:use|give (?:it|the file)))?\??", question, re.I)
    proposed = re.fullmatch(rf"(?:Should I use|Use|Save (?:it|the file) as)\s+[`\"']?({_NAME})[`\"']?(?:\s+for the (?:file\s*name|file))?\?", question, re.I)
    if not generic and not proposed:
        return False
    filename = proposed[1] if proposed else ""
    formats = {value.lower() for value in _FORMAT.findall(request)}
    if len(formats) != 1 or (filename and (not safe_filename(filename) or filename.rsplit(".", 1)[1].lower() not in formats)):
        return False
    with _lock:
        now = time.monotonic()
        for key in [key for key, draft in _pending.items() if draft["expires"] <= now]:
            _pending.pop(key, None)
        _pending[_key(sender, chat_id)] = {"request": request, "filename": filename, "format": next(iter(formats)), "expires": now + _TTL}
    return True


def observe(sender, chat_id, text, *, has_image=False, authorized=True):
    """Invalidate unrelated input before early/native handlers can return."""
    with _lock:
        key = _key(sender, chat_id)
        draft = _pending.get(key)
        if draft and (not authorized or has_image or draft["expires"] <= time.monotonic() or not (_answer(text) or _YES.fullmatch((text or "").strip()))):
            _pending.pop(key, None)


def clear_chat(chat_id):
    destination = _key("", chat_id)[1]
    with _lock:
        for key in [key for key in _pending if key[1] == destination]:
            _pending.pop(key, None)


def consume(sender, chat_id, text):
    """Return a bounded selection, clarification, or no match. Does not grant access."""
    with _lock:
        key = _key(sender, chat_id)
        draft = _pending.get(key)
        if not draft:
            return None
        if draft["expires"] <= time.monotonic():
            _pending.pop(key, None)
            return None
        filename = _answer(text)
        if not filename and _YES.fullmatch((text or "").strip()):
            filename = draft["filename"]
            if not filename:
                return ASK_NAME
        if not filename or filename.rsplit(".", 1)[1].lower() != draft["format"]:
            _pending.pop(key, None)
            return "That filename doesn't match the pending export. Nothing was written. Please send a new save request."
        # Consume BEFORE execution. Failed/uncertain tools or sends cannot replay.
        _pending.pop(key, None)
        return Selection(draft["request"], filename)


def argument_guard(selection):
    """Constrain the existing owner executor; model-selected paths are untrusted."""
    filename = selection.filename
    used = False

    def allow(name, args):
        nonlocal used
        allowed = bool(
            not used and name == "write_file" and isinstance(args, dict)
            and set(args) == {"path", "content"} and args.get("path") == filename
            and isinstance(args.get("content"), str) and safe_filename(filename)
            # Legacy writes resolve relative to cwd. Refuse another cwd or an
            # existing file (including symlinks); this follow-up is not overwrite consent.
            and os.path.realpath(os.getcwd()) == os.path.realpath(PROJECT_ROOT)
            and not os.path.lexists(PROJECT_ROOT / filename)
        )
        if allowed:
            used = True
        return allowed

    return allow
