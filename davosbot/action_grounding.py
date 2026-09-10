"""Conservative checks for external-action claims without an execution receipt.

This is response wording only. It never grants tools or infers that an earlier
action failed. Actual mutation receipts bypass this check in ToolTrace.
"""

import re
from .terminal_lookup import NO_LOOKUP_RESULT, is_terminal_lookup_placeholder


_TEXT_TASK = re.compile(
    r"^\s*(?:please\s+)?(?:translate\b|quote\b|role[ -]?play\b|pretend\b|imagine\b|"
    r"(?:write|draft|compose|rewrite)\b.{0,70}\b"
    r"(?:sentence|dialogue|story|scene|caption|reply|poem|joke|example)\b)", re.I,
)
_COMPLETED_OR_PROGRESS = (
    r"order(?:ed|ing)|purchas(?:ed|ing)|bought|book(?:ed|ing)|reserv(?:ed|ing)|"
    r"plac(?:ed|ing)|sent|sending|scheduled|scheduling|saved|saving|updated|updating|"
    r"changed|changing|applied|applying|removed|removing|disabled|enabled|deleted|"
    r"cancelled|canceled|logged|fixed|stripped|tested|verified|checked"
)
_ACTION = (
    _COMPLETED_OR_PROGRESS + r"|set up|"
    r"order|purchase|buy|book|reserve|place|send|schedule|save|update|change|apply|"
    r"remove|disable|enable|delete|cancel|log|fix|test|verify|check"
)
_ACTOR_CLAIM = re.compile(
    r"(?:^|[,;:]\s*)(?:I|we|meesa|Davos(?:Bot)?)(?:(?:'ve|'m|'ll|'re)|"
    r"\s+(?:have|am|are|will))?\s+"
    r"(?:(?:just|already|successfully|now|actually|really)\s+){0,3}"
    r"(?P<action>" + _ACTION + r")\b", re.I,
)
_BARE_CLAIM = re.compile(
    r"^\s*(?:(?:absolutely|done|sure|all set|yep|yes|confirmed)[,!: ]+)*"
    r"(?P<action>" + _COMPLETED_OR_PROGRESS + r")\b", re.I,
)
_PASSIVE_CLAIM = re.compile(
    r"\b(?:your|the|that|this|it|they)\s*(?:[\w -]{0,65}?)\s+"
    r"(?:is|are|was|were|has been|have been)\s+"
    r"(?:(?:now|already|successfully)\s+)?"
    r"(?P<action>ordered|placed|purchased|booked|reserved|sent|scheduled|saved|"
    r"updated|changed|applied|removed|disabled|enabled|deleted|cancelled|canceled|"
    r"logged|fixed|set up|stripped|tested|verified)\b", re.I,
)
_NONASSERTION = re.compile(
    r"^(?:if|suppose|when|once|after|before|to\b|you (?:can|could|should|need)|"
    r"(?:I|we) (?:can|could|would|need|cannot|can't|don't|didn't|haven't)|"
    r"(?:I|we)'d|according to|you (?:said|reported|told))\b", re.I,
)
_CHANGE_CONTEXT = re.compile(
    r"\b(?:cron|reminder|config(?:uration)?|settings?|intro|themes?|source|emoji|"
    r"personas?|memory|files?|saved|deplo(?:y|yed)|job|automation)\b", re.I,
)
_SCHEDULE_CONTEXT = re.compile(r"\b(?:cron|reminder|recurring|schedule|weekly|daily|job|automation)\b", re.I)
_ORDER_CONTEXT = re.compile(r"\b(?:orders?|buy|purchase|checkout|wings|food|pizza|restaurant|takeout|tickets?|hotel|flight|reservation)\b", re.I)
_MESSAGE_CONTEXT = re.compile(r"\b(?:text|message|email|DM|send|sent|sending|recipient)\b", re.I)
_TEST_CONTEXT = re.compile(r"\b(?:scripts?|code|tests?|suite|cron|reminder|job|settings?|configuration|automation)\b", re.I)
_VERIFY_CONTEXT = re.compile(r"\b(?:saved|persisted|deployed|live|runtime|cron|reminder|settings?|configuration|job|automation)\b", re.I)


def _readback_supports(context: str, tools) -> bool:
    scopes = {
        "list_crons": r"\b(?:cron|job)\b",
        "list_reminders": r"\breminder\b",
        "read_file": r"\b(?:file|settings?|config(?:uration)?)\b",
        "get_group_chat_status": r"\bgroup\b",
    }
    return any(name in tools and re.search(pattern, context, re.I) for name, pattern in scopes.items())


def ground_unexecuted_reply(user_msg: str, reply: str | None, *, readback_tools=()) -> str | None:
    """Replace clear fulfillment claims in no-action turns, preserving normal prose.

Quoted/fictional text, explanations, negations, and proposed actions are not
fulfillment. This deliberately does not classify every possible paraphrase.
"""
    if not reply or _TEXT_TASK.search(user_msg or ""):
        return reply
    if is_terminal_lookup_placeholder(user_msg, reply):
        return NO_LOOKUP_RESULT
    clean = reply.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    clean = re.sub(r"```.*?```", "", clean, flags=re.S)
    clean = re.sub(r'"[^"\n]*"', "", clean)
    clean = clean.replace("**", "").replace("__", "")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", clean):
        sentence = sentence.strip(" -*\t")
        if not sentence or sentence.endswith("?") or _NONASSERTION.search(sentence):
            continue
        # A recalled chat reply is not a newly executed outbound operation.
        if re.search(r"\b(?:above|below|previous reply|earlier in (?:this|our) chat)\b", sentence, re.I):
            continue
        match = _ACTOR_CLAIM.search(sentence) or _BARE_CLAIM.search(sentence) or _PASSIVE_CLAIM.search(sentence)
        if not match:
            continue
        action = match["action"].lower()
        context = (user_msg or "") + " " + sentence
        if match.re is _BARE_CLAIM and action.endswith("ing") and re.search(
            r"\b(?:is|can|may|requires|costs|means|helps)\b", sentence[match.end():], re.I,
        ):
            continue
        if re.search(r"\b(?:alphabetical|sorted list|order of operations|in order)\b", sentence, re.I):
            continue
        if action.startswith(("order", "purchas", "bought", "buy", "book", "reserv", "plac")) and _ORDER_CONTEXT.search(context):
            return "I don't have confirmation that an order or booking was placed. I can help prepare it or find the ordering page; an actual confirmation is needed before treating it as done."
        if action in {"sent", "sending", "send"} and _MESSAGE_CONTEXT.search(context):
            if re.search(r"\b(?:good vibes|love|thoughts|prayers)\b", sentence, re.I):
                continue
            return "I don't have a send confirmation for that message. I can draft it here, but a draft is not a delivered message."
        if action in {"tested", "verified", "checked", "test", "verify", "check"}:
            test_claim = action in {"tested", "test"} or re.search(r"\btests?\b.{0,40}\b(?:passed|successful|working)\b", sentence, re.I)
            if test_claim and _TEST_CONTEXT.search(context):
                return "I don't have a test execution result for that. Reading saved settings or a file does not prove the test ran or passed."
            if _VERIFY_CONTEXT.search(context) and not _readback_supports(context, readback_tools):
                return "I haven't verified that against saved settings or a real test. Check the saved job or settings before relying on the earlier claim."
            continue
        if action.startswith(("schedul", "sav", "updat", "chang", "appl", "remov", "disabl", "enabl", "delet", "cancel", "log", "fix", "set up", "stripp")) and _CHANGE_CONTEXT.search(context):
            if _SCHEDULE_CONTEXT.search(context):
                return "I don't have confirmation that the job was saved or changed. Use 'list crons' or 'list reminders' in this chat to check what is actually saved."
            return "I don't have confirmation that the change was saved. I can help draft the change, but the saved settings need to be checked before treating it as done."
    return reply
