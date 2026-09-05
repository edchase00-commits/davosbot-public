"""Named Work adapters for existing Davos features, with no general command route.

Arguments are untrusted transport data. The local owner is the only actor; no
caller can supply a sender, database path, shell command, or native password.
Imports are lazy so capability/schema discovery does not initialize the bot.
"""

from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
import json
import math
from pathlib import Path
import re
import time


def _field(kind, *, required=False, **limits):
    return {"type": kind, "required": required, **limits}


def _text(size=200, *, required=False, **limits):
    return _field("string", required=required, minLength=1, maxLength=size, **limits)


def _action(description, fields=None, *, mutates=False, **notes):
    return {"description": description, "fields": fields or {}, "mutates": mutates, **notes}


_ACK = _field("boolean", required=True, enum=[True])
_ID = _field("integer", required=True, minimum=1, maximum=2147483647)
_HANDLE = _text(254, required=True, pattern=r"(?:\+[1-9][0-9]{7,14}|[^\s@,;<>]+@[^\s@,;<>]+)")
_GROUP = _text(32, required=True, pattern=r"[0-9a-f]{32}")
_NAME = _text(80, required=True, pattern=r"[a-z0-9][a-z0-9 _-]{0,79}")

EXTRA_ACTIONS = {
    "workouts.log": _action("Log sets in the owner's existing workout journal", {
        "exercise_name": _text(120, required=True),
        "muscle_group": _text(40), "notes": _text(500),
        "sets": _field("array", required=True, minItems=1, maxItems=30,
                       items={"type": "object", "additionalProperties": False,
                              "properties": {"weight": {"type": "number", "minimum": 0, "maximum": 5000},
                                             "reps": {"type": "integer", "minimum": 1, "maximum": 1000}},
                              "required": ["weight", "reps"]}),
    }, mutates=True),
    "workouts.query": _action("Query the owner's workout journal", {
        "query_type": _text(required=True, enum=["recent", "exercise", "summary"]),
        "exercise": _text(120),
    }),
    "workouts.today": _action("Read today's native workout summary"),
    "workouts.summary": _action("Read the native weekly workout summary"),
    "workouts.plan": _action("Read the native workout suggestion from recent history"),
    "bets.log": _action("Record an existing sports bet; never places a wager", {
        "event": _text(200, required=True),
        "odds": _field("integer", required=True, minimum=-100000, maximum=100000),
        "stake_units": _field("number", required=True, minimum=0.001, maximum=100000),
        "notes": _text(500),
    }, mutates=True),
    "bets.stats": _action("Read the owner's native sports-bet record statistics", {
        "period": _text(enum=["week", "today", "month", "last7"]),
    }),
    "bets.settle": _action("Mark an exact sports-bet record as win, loss, or push; no money moves", {
        "bet_id": _ID, "result": _text(required=True, enum=["win", "loss", "push"]),
    }, mutates=True),
    "bets.social.list": _action("Read existing open social-bet records using the native admin gate"),
    "personas.list": _action("List available Davos personas"),
    "personas.status": _action("Read a separate disk snapshot of the owner's active DM persona"),
    "personas.set": _action("Select a persona in native iMessage; Work cannot safely mutate shared persona state", {
        "name": _text(80, required=True), "clear_history": _ACK,
    }, availability="native_confirmation_required"),
    "groups.list": _action("Read known group IDs from a separate disk snapshot; owner membership is required"),
    "groups.status": _action("Read a separate disk snapshot of an exact group containing the owner", {"chat_id": _GROUP}),
    "groups.set_enabled": _action("Enable or disable a group in native iMessage; shared state has no cross-thread lock", {
        "chat_id": _GROUP, "enabled": _field("boolean", required=True),
    }, availability="native_confirmation_required"),
    "access.status": _action("Read the owner's native permissions and active admin list"),
    "access.grant_admin": _action("Grant admin in native iMessage; the helper also changes shared group state", {
        "handle": _HANDLE, "acknowledge_access_change": _ACK,
    }, availability="native_confirmation_required"),
    "access.revoke_admin": _action("Revoke admin using the native owner gate for an exact handle", {
        "handle": _HANDLE, "acknowledge_access_change": _ACK,
    }, mutates=True),
    "access.set_approved": _action("Change group approval in native iMessage; shared state has no cross-thread lock", {
        "handle": _HANDLE, "approved": _field("boolean", required=True),
        "acknowledge_access_change": _ACK,
    }, availability="native_confirmation_required"),
    "skills.list": _action("List Davos's native canned-response skills"),
    "skills.create": _action("Create a native trigger/response skill, not executable code", {
        "name": _NAME, "trigger_phrase": _text(200, required=True),
        "response_template": _text(2000, required=True),
    }, mutates=True),
    "skills.set_enabled": _action("Enable or disable an exact native response skill", {
        "name": _NAME, "enabled": _field("boolean", required=True),
    }, mutates=True),
    "skills.update": _action("Content edits require a reviewed adapter; native helper only toggles enablement", {
        "name": _NAME, "trigger_phrase": _text(200), "response_template": _text(2000),
    }, availability="unsupported"),
    "changes.list": _action("Read the existing native change-log board"),
    "changes.intake": _action("Log a guarded Codex change request; does not edit code or deploy", {
        "request": _text(1800, required=True), "reason": _text(1800),
    }, mutates=True),
    "cleanup.start": _action(
        "Request one Mini safe-cleanup run of the existing backlog, like owner iMessage yes fix. "
        "Use only when the owner requests cleanup; log new issues with changes.intake first. "
        "Accepted means launch requested, not fixes completed. RED changes remain review-only.",
        {"acknowledge_backlog": _ACK}, mutates=True,
        status_action="cleanup.status", receipt_action="requests.receipt",
        scope="safe_backlog", automatic_retry=False),
    "cleanup.status": _action(
        "Read global cleanup status and remaining backlog counts. Not a request-specific receipt; "
        "a finished process does not prove any particular fix was deployed.",
        scope="global_cleanup_queue"),
    "changes.done": _action("Remove one completed change-log row using native done semantics", {
        "change_id": _ID, "acknowledge_removal": _ACK,
    }, mutates=True),
    "memory.note": _action("Add an owner-private structured memory note", {
        "text": _text(2000, required=True),
    }, mutates=True),
    "memory.search": _action("Search only owner-private structured notes, not memory files or chat history", {
        "query": _text(120, required=True),
        "limit": _field("integer", minimum=1, maximum=10),
    }),
    "private_send.prepare": _action("Use the owner's native iMessage password confirmation flow; chat cannot stage safely", {
        "recipient": _HANDLE, "message": _text(2000, required=True),
    }, availability="native_confirmation_required"),
    "images.generate": _action("Generate a new image with Davos's native access/quota gates; deliver to owner DM", {
        "prompt": _text(2000, required=True),
    }, mutates=True),
    "images.status": _action("Read active jobs in the default owner-DM image queue; does not confirm delivery"),
    "images.receipt": _action("Read a durable Work image job outcome by its original request; never retries or resends", {
        "request_id": _text(36, required=True, pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
        "request_comment_id": _field("integer", required=True, minimum=1, maximum=9007199254740991),
    }),
    "images.scan": _action("Read one owner-authorized PNG/JPEG uploaded as an immutable blob in the private Davos repository; requires Pillow and explicit GitHub retention consent", {
        "question": _text(1000, required=True),
        "image_blob_sha": _text(40, required=True, pattern=r"[0-9a-f]{40}"),
        "image_sha256": _text(64, required=True, pattern=r"[0-9a-f]{64}"),
        "mime_type": _text(10, required=True, enum=["image/png", "image/jpeg"]),
        "acknowledge_github_retention": _ACK,
    }, mutates=True, max_image_bytes=1048576, max_image_pixels=4194304,
       input_method="github_create_blob_in_fixed_private_repository"),
}


def _invalid():
    raise ValueError("invalid_action_arguments")


def _validate_field(value, rule):
    kind = rule["type"]
    if kind == "string":
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            _invalid()
        if len(value) < rule.get("minLength", 0) or len(value) > rule.get("maxLength", 2000):
            _invalid()
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            _invalid()
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            _invalid()
    elif kind == "boolean":
        if type(value) is not bool:
            _invalid()
    elif kind in {"integer", "number"}:
        if type(value) not in ((int,) if kind == "integer" else (int, float)):
            _invalid()
        try:
            if not math.isfinite(value):
                _invalid()
        except OverflowError:
            _invalid()
        if value < rule.get("minimum", -1e12) or value > rule.get("maximum", 1e12):
            _invalid()
    elif kind == "array":
        if not isinstance(value, list) or not rule["minItems"] <= len(value) <= rule["maxItems"]:
            _invalid()
        for item in value:
            item_rule = rule["items"]
            if not isinstance(item, dict) or set(item) != set(item_rule["required"]):
                _invalid()
            for name, field_rule in item_rule["properties"].items():
                _validate_field(item[name], field_rule)
    else:
        _invalid()
    if "enum" in rule and value not in rule["enum"]:
        _invalid()


def validate_extra_action(action, args):
    """Validate an exact named request without importing runtime modules."""
    if not isinstance(action, str) or action not in EXTRA_ACTIONS:
        raise ValueError("unsupported_action")
    if not isinstance(args, dict):
        _invalid()
    fields = EXTRA_ACTIONS[action]["fields"]
    if set(args) - set(fields):
        _invalid()
    for name, rule in fields.items():
        if name not in args:
            if rule.get("required"):
                _invalid()
            continue
        _validate_field(args[name], rule)
    if action == "workouts.query" and args["query_type"] == "exercise" and "exercise" not in args:
        _invalid()
    if action == "bets.log" and abs(args["odds"]) < 100:
        _invalid()
    if action == "personas.set" and ("\n" in args["name"] or "\t" in args["name"]):
        _invalid()
    if action == "memory.search" and (len(args["query"]) < 3 or re.search(r"[%_]", args["query"])):
        _invalid()


def _module(name):
    return import_module("." + name, __package__)


def _owner(owner):
    configured = _module("config").OWNER_ID
    if not isinstance(configured, str) or not configured or owner != configured:
        raise ValueError("owner_required")
    if not _module("permissions").is_owner(owner):
        raise ValueError("owner_required")
    return owner


def _result(text, *, success=None, status=None, evidence=None):
    """Preserve a native refusal as an error, never label failed mutations done."""
    text = str(text or "")[:8000]
    if status is None:
        if success is not None:
            status = "ok" if text.startswith(success) else "error"
        else:
            status = "error" if re.search(
                r"permission denied|\bfailed\b|\berror\b|not initialized|admin-only|owner.only|unknown persona",
                text, re.I,
            ) else "ok"
    if status == "unsupported":
        status = "error"
        evidence = {**(evidence or {}), "code": "unsupported"}
    result = {"status": status, "result": text}
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _group_state_snapshot():
    """Read an independent bounded snapshot; never reload shared group globals.

    The native writer replaces this file atomically. An independent reader does
    not interfere with its in-memory load/mutate/save sequence. Invalid reads
    fail closed rather than calling a native shared-state helper as fallback.
    """
    path = Path(_module("config").PROJECT_ROOT) / "gc_state.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise ValueError("group_snapshot_unavailable") from None
    try:
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError
        enabled = state.get("enabled_chats", [])
        personas = state.get("personas", {})
        if not isinstance(enabled, list) or len(enabled) > 1000:
            raise ValueError
        if not all(isinstance(value, str) and len(value) <= 128 for value in enabled):
            raise ValueError
        if not isinstance(personas, dict) or len(personas) > 1000:
            raise ValueError
        if not all(isinstance(key, str) and len(key) <= 128
                   and (value is None or isinstance(value, str) and len(value) <= 200)
                   for key, value in personas.items()):
            raise ValueError
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ValueError("group_snapshot_invalid") from None
    return {"enabled_chats": enabled, "personas": personas}


def _group_status(chat_id, owner, snapshot):
    if not re.fullmatch(r"[0-9a-f]{32}", chat_id) or not _module("imessage").is_owner_in_chat(chat_id, owner):
        raise ValueError("unknown_or_unowned_group")
    return {"chat_id": chat_id, "enabled": chat_id in snapshot["enabled_chats"],
            "persona": snapshot["personas"].get(chat_id) or "default"}


def _image_status(owner):
    """Read only the native queue's bounded metadata, never image history/files."""
    active = _module("main")._active_image_jobs_for_context(owner, owner, is_group=False)
    now = time.time()
    jobs = []
    for job in active[:1]:  # The native queue holds at most one job per context.
        if (job.get("sender") != owner or job.get("recipient") != owner
                or job.get("is_group") is not False or job.get("route_key") != ""):
            continue
        started = job.get("started_ts")
        elapsed = (int(now - started) if type(started) in (int, float)
                   and 0 < started <= now and math.isfinite(started) else None)
        job_id = job.get("job_id")
        provider = job.get("provider")
        jobs.append({
            "job_id": job_id if isinstance(job_id, str) and re.fullmatch(r"[0-9]{10,16}-[0-9]{4}", job_id) else None,
            "provider": provider if isinstance(provider, str) and provider in {"openai", "gemini", "local"} else "unknown",
            "elapsed_seconds": elapsed,
            # The native job has no recorded deadline; provider timeouts/ETAs
            # cannot establish a remaining deadline for the entire job.
            "timeout_remaining_seconds": None,
        })
    text = ("An active image job was observed in the default owner-DM queue."
            if jobs else "No active image job was observed in the default owner-DM queue.")
    return _result(text + " This does not establish whether an earlier image was delivered or failed.", status="ok", evidence={
        "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "active_job_count": len(jobs), "jobs": jobs, "delivery_state": "unknown",
    })


def execute_extra_action(action, args, owner):
    """Execute one reviewed adapter. Transport owns durable replay protection."""
    validate_extra_action(action, args)
    owner = _owner(owner)
    args = deepcopy(args)  # Native workout helper normalizes its input in place.

    if action == "cleanup.start":
        result = _module("commands")._confirmed_safe_cleanup_result(owner)
        if result.get("status") == "accepted":
            result = {**result, "result": (
                "Mini repair launch requested. The supervisor still has to admit the run. "
                "Use cleanup.status for global progress and changes.list for remaining items. "
                "A completion text is requested but not guaranteed; no fix is verified yet."
            )}
        return result
    if action == "cleanup.status":
        text = _module("commands")._cmd_cleanup_status(owner)
        return _result(text + "\nThis is global queue status, not proof that a particular repair completed.",
                       success="Codex safe cleanup status:",
                       evidence={"scope": "global_cleanup_queue", "repairs_verified": False,
                                 "request_specific": False})

    if action == "images.status":
        return _image_status(owner)
    if action == "images.receipt":
        from .work_image_receipts import receipt
        evidence = receipt(args["request_id"], args["request_comment_id"], owner)
        state = evidence["job_state"]
        messages = {
            "sent": "The native iMessage attachment send was verified and saved. Device delivery/read status remains unknown.",
            "failed": "This image job stopped before its attachment send. No automatic retry was made.",
            "queued": "This image job is queued in the current process.",
            "generating": "This image job is generating in the current process.",
            "sending": "This image job is attempting its iMessage send; completion is not yet verified.",
            "unknown": "This image outcome is unknown. Do not repeat the request or resend automatically.",
        }
        return _result(messages[state], status="ok", evidence=evidence)
    if action == "private_send.prepare":
        # The native pending dict has no lock shared with the iMessage handler.
        # Do not race it or overwrite a phone-originated confirmation request.
        return _result(
            "Send this private-message request directly to Davos in your owner iMessage chat. "
            "Davos will resolve the recipient and require its existing password confirmation there. "
            "Nothing was staged or sent by this Work request.",
            status="native_confirmation_required", evidence={"staged": False, "sent": False},
        )
    if action == "skills.update":
        return _result("Native skills support create and enable/disable. Content editing needs a reviewed adapter.", status="unsupported")
    if action == "images.scan":
        return _module("work_image_input").scan_uploaded_image(args, owner)
    if action in {"personas.set", "groups.set_enabled", "access.grant_admin", "access.set_approved"}:
        # Native group/persona routines share an unlocked global state. Even
        # their read helpers reload it. Do not race the iMessage handler, and do
        # not partially grant admin before its native allow-list synchronization.
        return _result(
            "Make this persona, group, or access change directly in Davos iMessage. "
            "Work cannot safely change the native shared group state until both paths use shared synchronization. "
            "No setting or access record was changed.",
            status="native_confirmation_required", evidence={"changed": False},
        )

    if action.startswith("workouts."):
        if action == "workouts.log":
            return _result(_module("tools")._workout_log_tool(args, sender=owner), success="Logged")
        if action == "workouts.query":
            return _result(_module("tools")._query_workout(args, sender=owner))
        commands = _module("commands")
        helpers = {"workouts.today": commands._cmd_workout,
                   "workouts.summary": commands._cmd_workout_summary,
                   "workouts.plan": commands._cmd_workout_plan}
        return _result(helpers[action](owner))

    if action.startswith("bets."):
        commands = _module("commands")
        if action == "bets.log":
            command = f"bet log {args['event']} {args['odds']:+d} {args['stake_units']}u"
            if args.get("notes"):
                command += " " + args["notes"]
            parsed = commands._parse_bet_input(command)
            if not isinstance(parsed, dict) or any(parsed[key] != expected for key, expected in (
                ("event", args["event"]), ("odds", args["odds"]),
                ("stake", args["stake_units"]), ("notes", args.get("notes", "")),
            )):
                return _result("Native bet parser could not preserve these exact fields; no record added.", status="error")
            return _result(commands._cmd_bet_log(command, owner), success="Bet #")
        if action == "bets.settle":
            response = commands._cmd_bet_settle(f"bet settle {args['bet_id']} {args['result']}", owner)
            confirmed = response.startswith((f"Bet #{args['bet_id']} settled as loss:", f"Bet #{args['bet_id']} pushed"))
            # Native owner/admin semantics can settle another user's exact ID.
            if args["result"] == "win":
                confirmed = bool(re.match(r"^\S+ cashed: \+\d+\.\d{2}u \(\+\$\d+\.\d{2}\) on ", response))
            return _result(response, status="ok" if confirmed else "error")
        if action == "bets.stats":
            period = {"week": "", "today": "today", "month": "month", "last7": "last 7"}[args.get("period", "week")]
            return _result(commands._cmd_bet_stats("bet stats " + period, owner))
        return _result(commands._cmd_bets("bets", owner))

    if action.startswith("personas."):
        available = _module("personality").list_personas()[:100]
        if action == "personas.list":
            return {"status": "ok", "result": available}
        current = _group_state_snapshot()["personas"].get("dm") or "default"
        display = current if current == "default" or current in available else "hidden persona"
        return {"status": "ok", "result": {"current": display, "available": available},
                "evidence": {"source": "independent_disk_snapshot"}}

    if action.startswith("groups."):
        snapshot = _group_state_snapshot()
        if action == "groups.list":
            known = set(snapshot["enabled_chats"]) | set(snapshot["personas"])
            rows = []
            for chat_id in sorted(known):
                if re.fullmatch(r"[0-9a-f]{32}", chat_id) and _module("imessage").is_owner_in_chat(chat_id, owner):
                    rows.append(_group_status(chat_id, owner, snapshot))
                if len(rows) >= 100:
                    break
            return {"status": "ok", "result": rows, "evidence": {"source": "independent_disk_snapshot"}}
        return {"status": "ok", "result": _group_status(args["chat_id"], owner, snapshot),
                "evidence": {"source": "independent_disk_snapshot"}}

    if action.startswith("access."):
        commands = _module("commands")
        if action == "access.status":
            return _result("Owner access verified.\n" + commands._cmd_admins(owner))
        handle = commands._parse_access_handle(args["handle"])
        if handle != args["handle"]:
            raise ValueError("exact_handle_required")
        return _result(commands._cmd_revoke("revoke " + handle, owner),
                       success=("Revoked admin from ", handle + " is not an active admin."))

    if action.startswith("skills."):
        commands = _module("commands")
        if action == "skills.list":
            return _result(commands._cmd_skills(owner))
        if action == "skills.create":
            return _result(commands.create_skill(owner, args["name"], args["trigger_phrase"], args["response_template"]),
                           success=f"Skill '{args['name']}' created.")
        mode = "enable" if args["enabled"] else "disable"
        return _result(commands._cmd_skill_manage(f"skill {mode} {args['name']}", owner),
                       success=f"Skill '{args['name']}' {mode}d.")

    if action.startswith("changes."):
        if action == "changes.intake":
            return _result(_module("change_request_tools")._log_change_request(args["request"], args.get("reason", "")),
                           success="Logged guarded Codex handoff #")
        commands = _module("commands")
        if action == "changes.list":
            return _result(commands._cmd_log("log board", sender=owner))
        return _result(commands._cmd_log(f"log done {args['change_id']}", sender=owner),
                       success=f"Log #{args['change_id']} removed.")

    if action.startswith("memory."):
        memory = _module("memory")
        if action == "memory.note":
            note_id = memory.add_owner_memory_item(args["text"], source="work_chat_owner_note")
            return {"status": "ok", "result": "Owner-private memory note saved.", "evidence": {"note_id": note_id}}
        rows = memory.search_owner_memory_items(args["query"], limit=args.get("limit", 5))
        return {"status": "ok", "result": [
            {"id": row["id"], "text": str(row["text"])[:2000], "timestamp": row["timestamp"]}
            for row in rows[:args.get("limit", 5)]
        ]}

    if action == "images.generate":
        main = _module("main")
        text = "image generate " + args["prompt"]
        # The native route also handles resends and phone-image followups. Reject
        # those before invoking it so Work cannot implicitly read Mini images.
        if (_module("image_conversation").is_image_followup(text)
                or main._IMAGE_QUEUE_STATUS_RE.search(text)
                or main._IMAGE_QUEUE_SEND_RE.search(text)
                or main._LAST_GENERATED_IMAGE_RE.search(text)):
            return _result("Use a new standalone image description. Existing-image references require native iMessage attachments.", status="unsupported")
        intent = _module("openai_images").parse_openai_image_intent(text, has_image=False)
        if intent is None or intent.kind != "generate":
            return _result("Could not resolve a new-image generation request.", status="error")
        from .work_image_receipts import ImageTracker
        tracking = ImageTracker(owner)
        response = main._handle_openai_image_intent(owner, text, None, owner, is_group=False, tracking=tracking)
        accepted = tracking.job_id is not None
        if not accepted and response and response.startswith("1 active image job;"):
            return _result("Another image job is already active. This request did not start a new job.", status="error",
                           evidence={"code": "image_queue_busy", "started": False})
        return _result(response or "Image generation did not start.", status="accepted" if accepted else "error",
                       evidence={"delivery": "native_owner_imessage", "delivery_confirmed": False,
                                 "job_id": tracking.job_id, "receipt_action": "images.receipt"})

    raise ValueError("unsupported_action")
