"""Narrow, deterministic acute-risk replies. No I/O, diagnosis or stored state.

History is caller-supplied and same-chat. It has no timestamps, so continuity is
turn-bounded, not age-aware. Unrelated topics and explicit resets break carryover.
"""

import re
from dataclasses import dataclass

HISTORY_LIMIT = 12
_MAX_TEXT_CHARS = 4000

_RESET = re.compile(
    r"^(?:memory clear\b|/?(?:forget|clear|delete|erase) (?:(?:all|my|the) )*(?:history|messages?|chat|last|everything)\b|persona\b|(?:reset|clear) (?:the )?persona\b"
    r"|(?:switch|change)(?: persona)? to\b|(?:default|normal)(?: mode| persona)?[.!]?$)", re.I,
)
_NONLITERAL = re.compile(
    r"```|\b(?:fictional (?:scene|story|character)|for (?:a|my) (?:story|novel|screenplay)"
    r"|writing (?:a |my )?(?:fiction|scene|novel|screenplay)|in (?:a|my|the) (?:novel|screenplay|video game)"
    r"|unit test|test case|regression test|quoted (?:text|message)|(?:review|audit|classify|analyze|analyse) (?:this|the) (?:historical |quoted )?(?:chat log|message|quote|transcript)"
    r"|in (?:minecraft|fortnite|warzone|gta)|dying (?:of|from) laughter|kill myself laughing)\b", re.I,
)
_PAST = re.compile(r"\b(?:last (?:year|month|week)|years? ago|used to)\b", re.I)
_FICTION_FRAME = re.compile(r"\b(?:fictional (?:tv )?scene|writing (?:a |my )?(?:fiction|novel|screenplay))\b", re.I)
_CHARACTER_LINE = re.compile(r"\b(?:the|a) character (?:says|said|asks)\b|^[\"'].*[\"'][.!]?$", re.I)
_HYPOTHETICAL = re.compile(
    r"\bhypothetical(?:ly)?\b|^(?:what|how)\b.*\bif\b.*\b(?:uses|takes|were|would)\b", re.I,
)
_REAL_FRAME = re.compile(
    r"^(?:(?:but|actually)[, ]+)?(?:in real life|this is real(?: now)?|this is happening now)\b[: ,]*", re.I,
)
_SELF_HARM = re.compile(
    r"\bi(?:'m|m| am) (?:going|planning|about) to (?:kill|hurt) myself\b"
    r"|\bi (?:want|plan|intend) to (?:kill myself|end my life)\b"
    r"|\bi(?:'m| am) suicidal\b|\bi have (?:a )?(?:suicide plan|plan to kill myself)\b", re.I,
)
_HUMAN = re.compile(r"\b(?:i|we|he|she|someone|my (?:friend|roommate|passenger|partner|brother|sister))\b", re.I)
_OPIOID = re.compile(r"\b(?:opioids?|heroin|fentanyl|oxycodone|oxy|morphine)\b", re.I)
_DANGER = re.compile(
    r"\b(?:unconscious|unresponsive|(?:won't|cannot|can't|couldn't|hasn't) (?:wake(?:n)? up|stay awake)"
    r"|not breathing|stopped breathing|slow (?:or irregular )?breathing|breathing (?:is )?(?:getting )?(?:(?:really|very) )?slow"
    r"|blue lips|overdos(?:e|ed|ing))\b", re.I,
)
_CRASH = re.compile(r"\b(?:crashed|car crash|car wreck|collision)\b", re.I)
_TRAUMA = re.compile(r"\b(?:killed|bleeding heavily|seriously (?:hurt|injured))\b", re.I)
_ALCOHOL = re.compile(r"\b(?:alcohol|drinking|drank|drunk|pregame|pregamed|beer|vodka|whiskey|tequila)\b", re.I)
_HEAVY_DRINKING = re.compile(
    r"\b(?:pregamed? too hard|drank (?:way )?too much|had too much (?:alcohol|beer|vodka|whiskey|tequila)"
    r"|(?:really|very|too) drunk|alcohol poisoning)\b", re.I,
)
_CONTINUATION = re.compile(
    r"^(?:(?:no|nah|so|okay|ok)[, .!]+)?(?:"
    r"(?:go |get )?back to\b.*(?:mode|jokes|banter|normal|protocol)"
    r"|(?:be|act|sound)(?: the)? (?:funny|funnier|chill|normal|casual)\b.*"
    r"|(?:give me more davos energy|hang loose|decatur (?:behavior|energy|emojis))\b.*"
    r"|(?:it's |it is )?(?:chill|fine|just (?:my |our )?(?:party |atl )?vibes)\b.*"
    r"|.*\b(?:ignore (?:him|her|them)|just (?:testing|joking)|we(?:'re| are) (?:just )?testing)\b.*"
    r"|.*\b(?:be|keep it) funny\b.*"
    r"|(?:what (?:should|do) (?:i|we) do|what now|is that safe|can i drive)[?.! ]*"
    r"|(?:so )?.*\b(?:burger|coffee|shower)\b.*\b(?:fix|sober|drive|cure)\b.*"
    r"|(?:he|she|they) (?:still |hasn't |haven't |won't |isn't |aren't ).*\b(?:wake|woken|breathing|awake)\b.*"
    r"|(?:i|we) (?:did not|didn't|haven't|have not) call ?911\b.*"
    r"|(?:ok|okay|lol|lmao|haha|thanks|cool|bet)[.! ]*"
    r")$", re.I,
)
_SAFE_CLAIM = re.compile(r"\bi(?:'m| am) (?:safe|fine|okay|ok)\b", re.I)
_SELF_RESOLVED = re.compile(
    r"\bi(?:'m| am) safe now\b.*\b(?:with (?:my |a )?(?:friend|sister|brother|partner|doctor|therapist)"
    r"|(?:not|no longer) (?:going|planning) to (?:hurt|kill) myself|no (?:plan|intent|intention))\b", re.I,
)
_CARE = re.compile(
    r"\b(?:paramedics|emts|doctors|emergency responders) (?:are |have arrived and are |are here and (?:are )?)"
    r"(?:here (?:and )?)?(?:treating|helping|taking care of)\b", re.I,
)
_HANDLE_PREFIX = re.compile(r"^\s*(\+?[\d ()-]{7,24}|[^:\s]+@[^:\s]+):\s*")
_SUBJECT = re.compile(r"\b(?:my (?:friend|roommate|passenger|partner|brother|sister|mother|father|grandma)|i|he|she|someone)\b", re.I)

_REPLIES = {
    "self_harm": (
        "For the person who described wanting to hurt themselves: I'm sorry you're facing this. "
        "If you may act on this or have already hurt yourself, call emergency services now (911 in the US), "
        "and ask a trusted person to stay with you. Move away from anything you could use to hurt yourself if you can do so safely. "
        "In the US, call or text 988 for crisis support. Are you safe right now?"
    ),
    "opioid": (
        "For the person with possible opioid overdose symptoms: call emergency services now (911 in the US). "
        "Ask someone nearby to give naloxone if available, stay with the person, and follow the dispatcher's instructions. "
        "Slow breathing or being unable to wake up needs urgent help; do not wait for another reply here."
    ),
    "trauma": (
        "For the injured person from the crash: call emergency services now (911 in the US) and give your location. "
        "Do not drive after drinking. Stay out of traffic, avoid moving the injured person unless needed for immediate safety or dispatcher-directed lifesaving care, "
        "and follow the dispatcher's instructions. Is help on the way?"
    ),
    "alcohol_emergency": (
        "For the person who is unresponsive or breathing abnormally after drinking: call emergency services now (911 in the US). "
        "Stay with them and follow the dispatcher's instructions; if they are breathing, keep them on their side to reduce choking risk. "
        "Do not give more alcohol or try food, coffee, or a shower as a cure."
    ),
    "alcohol_concern": (
        "Stop drinking and do not drive. Ask a sober person to stay with you. "
        "If someone cannot wake up, has slow or irregular breathing, seizures, or severe confusion, call emergency services now (911 in the US). "
        "Food, coffee, or showers do not sober you up. Are you fully awake and breathing normally?"
    ),
}


@dataclass(frozen=True)
class _Risk:
    kind: str
    reporter: str
    self_target: bool
    target: str = ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\u2019", "'").replace("\u2018", "'")).strip()


def _speaker_key(speaker: str) -> str:
    return re.sub(r"[ ()-]", "", speaker or "").lower()


def _literal_text(text: str, *, established_fiction: bool = False) -> str:
    """Scope game/history framing to its clause, with explicit real overrides.

    Audit/fiction/hypothetical framing carries through quoted following clauses
    until an explicit new real-life disclosure. An incidental game sentence
    does not label the next person's medical condition as fictional.
    """
    kept = []
    framed = established_fiction
    for clause in re.split(r"(?<=[.!?])\s+", text):
        real = _REAL_FRAME.match(clause)
        if real:
            framed = False
            clause = clause[real.end():]
        else:
            nonliteral = _NONLITERAL.search(clause)
            if "```" in clause or _FICTION_FRAME.search(clause) or _HYPOTHETICAL.search(clause):
                framed = True
                continue
            if nonliteral:
                scoped = bool(re.match(r"(?:in (?:minecraft|fortnite|warzone|gta)|dying |kill myself laughing)", nonliteral.group(), re.I))
                if not scoped:
                    framed = True
                continue
        if framed:
            continue
        if _PAST.search(clause) and not re.search(r"\b(?:just now|right now|today|again)\b", clause, re.I):
            continue
        kept.append(clause)
    return " ".join(kept).strip()


def _subject_before(text: str, position: int) -> str:
    subjects = [match.group().lower() for match in _SUBJECT.finditer(text[:position])]
    if not subjects:
        return ""
    if subjects[-1] in {"he", "she"}:
        return next((subject for subject in reversed(subjects[:-1]) if subject.startswith("my ")), subjects[-1])
    return subjects[-1]


def _linked_cause(pattern: re.Pattern, text: str, target: str) -> bool:
    """Do not turn another patient's medication, or a negated cause, into ours."""
    for match in pattern.finditer(text):
        if re.search(r"\b(?:no|not|without)\b[^.;!?]{0,24}$", text[:match.start()], re.I):
            continue
        cause_target = _subject_before(text, match.start())
        if target and cause_target == target:
            return True
    return False


def _risk(text: str, speaker: str) -> _Risk | None:
    if not text or len(text) > _MAX_TEXT_CHARS or _RESET.match(text):
        return None
    # An incidental past event or game in one sentence cannot hide a literal
    # emergency in the next. Retain current cause/symptom clauses together.
    text = _literal_text(text)
    harm = _SELF_HARM.search(text)
    if harm:
        prefix = re.split(r"[.!?]", text[:harm.start()])[-1].strip(" ,\"'")
        subjects = list(_SUBJECT.finditer(prefix))
        target = subjects[-1].group().lower() if subjects else ""
        quoted_report = bool(re.search(r"\b(?:said|says|texted|wrote|quoted|told|reports?)\b", prefix, re.I))
        direct_self = not quoted_report and (prefix.lower() in {"", "help", "please help"} or target == "i")
        return _Risk("self_harm", speaker, direct_self, target)
    if not _HUMAN.search(text):
        return None
    danger = next((match for match in _DANGER.finditer(text)
                   if not re.search(r"\b(?:not|never|isn't)\s+$", text[:match.start()], re.I)), None)
    trigger = danger or _TRAUMA.search(text) or _HEAVY_DRINKING.search(text)
    target = _subject_before(text, trigger.start() if trigger else len(text))
    self_target = target == "i"
    if danger and _linked_cause(_OPIOID, text, target):
        return _Risk("opioid", speaker, self_target, target)
    if _CRASH.search(text) and (danger or _TRAUMA.search(text)):
        return _Risk("trauma", speaker, False, target)
    if danger and _linked_cause(_ALCOHOL, text, target):
        return _Risk("alcohol_emergency", speaker, self_target, target)
    if _HEAVY_DRINKING.search(text):
        return _Risk("alcohol_concern", speaker, self_target, target)
    return None


def needs_context(text: str) -> bool:
    """Cheap routing candidate, never itself evidence of an emergency."""
    text = _normalize(text)
    if len(text) > _MAX_TEXT_CHARS or _RESET.match(text):
        return False
    return _risk(text, "") is not None or bool(_CONTINUATION.fullmatch(_literal_text(text)))


def _resolved(text: str, speaker: str, risk: _Risk) -> bool:
    # A concrete care report can come from a witness, unlike an unsupported
    # dismissal. Do not accept negated help or the reporter's own safety when
    # they originally described somebody else being at risk.
    care = _CARE.search(text)
    negated_care = care and re.search(
        r"\b(?:not|never|doubt|maybe|supposedly|if|might|haven't|aren't|isn't)\b[^.;!?]{0,32}$", text[:care.start()], re.I,
    )
    if care and not negated_care and "?" not in re.split(r"[.;!]", text[care.start():])[0]:
        patient = text[care.end():].strip().lower()
        contextual = bool(re.match(r"(?:him|her|them|the (?:injured )?person|my friend)\b", patient))
        same_target = bool(risk.target and patient.startswith(risk.target))
        self_care = risk.self_target and speaker == risk.reporter and bool(re.match(r"me\b", patient))
        if contextual or same_target or self_care:
            return True
    return bool(risk.kind == "self_harm" and risk.self_target and speaker and speaker == risk.reporter and _SELF_RESOLVED.search(text))


def acute_safety_reply(text: str, history: list[dict], *, sender: str, is_group: bool = False) -> str | None:
    """Return a fixed reply only for current risk or immediate risk continuity."""
    text = _normalize(text)
    if not needs_context(text):
        return None
    sender = _speaker_key(sender)
    active = None
    fiction = False
    for turn in (history or [])[-HISTORY_LIMIT:]:
        if not isinstance(turn, dict) or turn.get("role") != "user":
            continue
        content = _normalize(turn.get("content", ""))
        match = _HANDLE_PREFIX.match(content) if is_group else None
        speaker = _speaker_key(match.group(1)) if match else ("" if is_group else sender)
        if match:
            content = content[match.end():]
        if fiction and _CHARACTER_LINE.search(content):
            content = _literal_text(content, established_fiction=True)
            if not content:
                continue
        current = _risk(content, speaker)
        if current:
            active = current
            fiction = False
        elif _FICTION_FRAME.search(content) and not _literal_text(content):
            fiction = True
            active = None
            continue
        elif active and _resolved(content, speaker, active):
            active = None
        elif _SAFE_CLAIM.search(content) and active:
            # A different person's reassurance is not resolution for the
            # affected person. Keep a neutral subject in the resulting reply.
            continue
        elif not _CONTINUATION.fullmatch(_literal_text(content)):
            active = None
            fiction = False
    if fiction and _CHARACTER_LINE.search(text):
        text = _literal_text(text, established_fiction=True)
        if not text:
            return None
    current = _risk(text, sender)
    if current:
        active = current
    elif active and _resolved(text, sender, active):
        active = None
    return _REPLIES[active.kind] if active else None
