"""Authenticated, named Work requests over one pinned private GitHub issue.

GitHub comments are data, never shell commands. A durable started record is
written before an adapter runs. An interrupted action is reported as ambiguous
and is never automatically executed again. Publishing its receipt is separate.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .package_delivery import GitHubBridge, DeliveryError

REPOSITORY = "example/davosbot"
REPOSITORY_ID = 1220482736
GITHUB_OWNER_ID = 262686493
ISSUE_NUMBER = 129
ISSUE_ID = 5769576706
PREVIOUS_ISSUE_NUMBER = 64
PREVIOUS_ISSUE_ID = 5354864924
CHANNEL_STATE_MAX_BYTES = 262_144
ISSUE_URL = f"https://api.github.com/repos/{REPOSITORY}/issues/{ISSUE_NUMBER}"
COMMENT_ENDPOINT = f"repos/{REPOSITORY}/issues/{ISSUE_NUMBER}/comments"
POLL_SECONDS = 25
REQUEST_TTL = 900
RESULT_RETRY_SECONDS = 120
RETENTION_SECONDS = 7 * 86400
MAX_REQUEST_BYTES = 16_384
MAX_RESULT_BYTES = 49_152
MAX_RESPONSE_BYTES = 60_000
MAX_STATE_BYTES = 8_388_608
MAX_RECORDS = 5000
MAX_PAGES = 50
PAGE_SIZE = 2  # Even escaped supplementary Unicode bodies fit the 2 MiB gh cap.
MAX_ACTIONS_PER_POLL = 10
REQUEST_FIELDS = {"schema_version", "kind", "request_id", "action", "args"}
RESULT_FIELDS = {"schema_version", "kind", "request_id", "request_comment_id", "state", "result", "runtime_revision", "completed_at"}
logger = logging.getLogger(__name__)
_start_lock = threading.Lock()
_thread = None

COMMENT_QUERY = """query($id:ID!){node(id:$id){__typename ... on IssueComment {
 fullDatabaseId body createdAt updatedAt lastEditedAt includesCreatedEdit createdViaEmail
 author { __typename ... on User { databaseId } } editor { __typename }
 issue { fullDatabaseId number repository { databaseId nameWithOwner isPrivate
 owner { __typename ... on User { databaseId } } } }
}}}"""

COMMENTS_QUERY = """query($after:String,$first:Int!,$issueNumber:Int!){
 repository(owner:"<windows-user>se00-commits",name:"davosbot") {
 databaseId nameWithOwner isPrivate owner { __typename ... on User { databaseId } }
 issue(number:$issueNumber) { fullDatabaseId number comments(first:$first,after:$after) {
 edges { cursor node { id fullDatabaseId body createdAt updatedAt
 author { __typename ... on User { databaseId } } } }
 pageInfo { endCursor hasNextPage }
 } }
 }}"""


class BridgeError(Exception):
    """Fixed, non-secret operational error code."""


class RequestRejected(BridgeError):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _parse(raw, maximum):
    if not isinstance(raw, (bytes, str)) or len(raw.encode() if isinstance(raw, str) else raw) > maximum:
        raise BridgeError("payload_too_large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalid_json") from None


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise RequestRejected("invalid_server_timestamp") from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid4(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _big_id(value, expected):
    return type(value) in (int, str) and str(value) == str(expected)


def _valid_cursor(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_+/=-]{1,1024}", value))


def _load_cursor(path):
    """A missing scan checkpoint safely replays discovery, never saved actions."""
    if path.is_symlink():
        raise BridgeError("unsafe_state_path")
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            value = _parse(handle.read(4097), 4096)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "cursor"} or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                (value["cursor"] is not None and not _valid_cursor(value["cursor"]))):
            raise ValueError
        return value["cursor"]
    except (BridgeError, ValueError, TypeError, OSError):
        raise BridgeError("scan_state_corrupt") from None


# gh can rewrite escaped C0/C1 controls even inside a nested JSON body.
# Normalize new result values before the durable journal, never legacy records.
_ANSI_OUTPUT = re.compile(r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[@-_])")
_LITERAL_CONTROL = re.compile(r"\\u00([0-9a-f]{2})", re.IGNORECASE)


def _unsafe_control(code):
    return (code < 32 and code not in (9, 10, 13)) or 127 <= code <= 159


def _transport_stable_text(text):
    text = _ANSI_OUTPUT.sub("", text)
    text = "".join("[control]" if _unsafe_control(ord(char)) else char for char in text)
    return _LITERAL_CONTROL.sub(
        lambda match: "[escaped-control]" if _unsafe_control(int(match.group(1), 16)) else match.group(0), text)


def safe_result(value, redactor=None):
    """Bound and redact adapter output before it reaches disk or GitHub."""
    sensitive = re.compile(r"password|secret|token|credential|api_key|authorization|cookie|private_key", re.I)
    tokens = re.compile(r"(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~-]+", re.I)
    def clean(item, depth=0):
        if depth > 12:  # Full capability JSON schemas include nested array objects.
            raise ValueError
        if isinstance(item, dict):
            if len(item) > 100 or any(not isinstance(key, str) or len(key) > 100 or _transport_stable_text(key) != key for key in item):
                raise ValueError
            return {key: "[redacted]" if sensitive.search(key) else clean(val, depth + 1) for key, val in item.items()}
        if isinstance(item, list):
            if len(item) > 100:
                raise ValueError
            return [clean(val, depth + 1) for val in item]
        if isinstance(item, str):
            # Formatting removal can join split credentials. Normalize before
            # every security filter, then keep a final transport-stability pass.
            text = _transport_stable_text(item)
            text = redactor(text) if redactor is not None else text
            text = tokens.sub("[redacted]", text)
            text = re.sub(r"-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*", "[redacted]", text)
            return _transport_stable_text(text)
        if item is None or type(item) in (bool, int) or (type(item) is float and math.isfinite(item)):
            return item
        raise ValueError
    if not isinstance(value, dict):
        raise ValueError
    result = clean(value)
    if len(_json_bytes(result)) > MAX_RESULT_BYTES:
        raise ValueError
    return result


def parse_request(body):
    request = _parse(body, MAX_REQUEST_BYTES)
    if (not isinstance(request, dict) or set(request) != REQUEST_FIELDS or
            type(request["schema_version"]) is not int or request["schema_version"] != 1 or
            request["kind"] != "davos_request" or not _uuid4(request["request_id"]) or
            not isinstance(request["action"], str) or
            not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", request["action"]) or
            not isinstance(request["args"], dict)):
        raise RequestRejected("invalid_request_schema")
    return request


def _owned_comment(comment):
    return (isinstance(comment, dict) and type(comment.get("id")) is int and comment["id"] > 0 and
            isinstance(comment.get("user"), dict) and comment["user"].get("type") == "User" and
            type(comment["user"].get("id")) is int and comment["user"]["id"] == GITHUB_OWNER_ID and
            comment.get("issue_url") == ISSUE_URL and
            comment.get("url") == f"https://api.github.com/repos/{REPOSITORY}/issues/comments/{comment['id']}")


class GitHubTransport:
    """Only pinned repository/issue requests; gh handles existing credentials."""

    def __init__(self, root):
        self.client = GitHubBridge(root)

    def _call(self, arguments, payload=None):
        try:
            return _parse(self.client._call(arguments, payload), 2_097_152)
        except DeliveryError:
            raise BridgeError("github_unavailable") from None

    def assert_channel(self):
        repo = self._call([f"repos/{REPOSITORY}", "--method", "GET"])
        if (not isinstance(repo, dict) or repo.get("private") is not True or
                repo.get("id") != REPOSITORY_ID or repo.get("full_name") != REPOSITORY or
                not isinstance(repo.get("owner"), dict) or repo["owner"].get("id") != GITHUB_OWNER_ID):
            raise BridgeError("channel_repository_mismatch")
        actor = self._call(["user", "--method", "GET"])
        if (not isinstance(actor, dict) or actor.get("type") != "User" or
                type(actor.get("id")) is not int or actor["id"] != GITHUB_OWNER_ID or
                actor.get("login") != REPOSITORY.split("/", 1)[0]):
            raise BridgeError("runtime_github_owner_mismatch")
        issue = self._call([f"repos/{REPOSITORY}/issues/{ISSUE_NUMBER}", "--method", "GET"])
        if (not isinstance(issue, dict) or issue.get("id") != ISSUE_ID or
                issue.get("number") != ISSUE_NUMBER or issue.get("url") != ISSUE_URL or
                "pull_request" in issue or issue.get("state") not in {"open", "closed"}):
            raise BridgeError("channel_issue_mismatch")
        return issue["state"] == "open"

    def comment_pages(self, cursor):
        """Bound each poll, retaining an opaque cursor instead of a page offset.

        A full page is not an error. The caller checkpoints consumed edges and
        resumes next poll; neither old pending receipts nor same-second bursts
        can pin every poll to the first 1,000 comments.
        """
        seen = {cursor}
        for _ in range(MAX_PAGES):
            response = self._call(["graphql", "--method", "POST"],
                                  {"query": COMMENTS_QUERY, "variables": {"after": cursor, "first": PAGE_SIZE, "issueNumber": ISSUE_NUMBER}})
            if not isinstance(response, dict) or response.get("errors"):
                raise BridgeError("comments_scan_unavailable")
            repo = (response.get("data") or {}).get("repository")
            issue = repo.get("issue") if isinstance(repo, dict) else None
            owner = repo.get("owner") if isinstance(repo, dict) else None
            if (not isinstance(repo, dict) or repo.get("databaseId") != REPOSITORY_ID or
                    repo.get("nameWithOwner") != REPOSITORY or repo.get("isPrivate") is not True or
                    not isinstance(owner, dict) or owner.get("__typename") != "User" or owner.get("databaseId") != GITHUB_OWNER_ID or
                    not isinstance(issue, dict) or not _big_id(issue.get("fullDatabaseId"), ISSUE_ID) or issue.get("number") != ISSUE_NUMBER):
                raise BridgeError("channel_issue_mismatch")
            connection = issue.get("comments")
            edges = connection.get("edges") if isinstance(connection, dict) else None
            info = connection.get("pageInfo") if isinstance(connection, dict) else None
            if (not isinstance(edges, list) or len(edges) > PAGE_SIZE or not isinstance(info, dict) or
                    type(info.get("hasNextPage")) is not bool):
                raise BridgeError("invalid_comments_page")
            rows = []
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                token = edge.get("cursor") if isinstance(edge, dict) else None
                if (not _valid_cursor(token) or token in seen or not isinstance(node, dict) or
                        not re.fullmatch(r"[1-9][0-9]{0,19}", str(node.get("fullDatabaseId", "")))):
                    raise BridgeError("invalid_comments_page")
                comment_id = int(node["fullDatabaseId"])
                author = node.get("author") or {}
                rows.append({"id": comment_id, "node_id": node.get("id"), "body": node.get("body"),
                             "created_at": node.get("createdAt"), "updated_at": node.get("updatedAt"),
                             "user": {"id": author.get("databaseId"), "type": author.get("__typename")},
                             "issue_url": ISSUE_URL,
                             "url": f"https://api.github.com/repos/{REPOSITORY}/issues/comments/{comment_id}",
                             "scan_cursor": token})
                seen.add(token)
            if ((edges and info.get("endCursor") != edges[-1]["cursor"]) or
                    (info["hasNextPage"] and not edges)):
                raise BridgeError("invalid_comments_page")
            yield rows, not info["hasNextPage"]
            if not info["hasNextPage"]:
                return
            cursor = info["endCursor"]

    def authenticate(self, comment, *, receipt=False):
        if not _owned_comment(comment):
            raise RequestRejected("unauthorized_comment")
        node_id = comment.get("node_id")
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", node_id):
            raise RequestRejected("invalid_comment_node")
        response = self._call(["graphql", "--method", "POST"], {"query": COMMENT_QUERY, "variables": {"id": node_id}})
        if not isinstance(response, dict) or response.get("errors"):
            raise BridgeError("comment_auth_unavailable")
        node = response.get("data", {}).get("node")
        if not isinstance(node, dict):
            raise BridgeError("comment_auth_unavailable")
        author, issue = node.get("author"), node.get("issue")
        repo = issue.get("repository") if isinstance(issue, dict) else None
        owner = repo.get("owner") if isinstance(repo, dict) else None
        if (node.get("__typename") != "IssueComment" or not _big_id(node.get("fullDatabaseId"), comment["id"]) or
                node.get("body") != comment.get("body") or
                node.get("createdAt") != comment.get("created_at") or node.get("updatedAt") != comment.get("updated_at") or
                not isinstance(author, dict) or author.get("__typename") != "User" or author.get("databaseId") != GITHUB_OWNER_ID or
                not isinstance(issue, dict) or issue.get("number") != ISSUE_NUMBER or not _big_id(issue.get("fullDatabaseId"), ISSUE_ID) or
                not isinstance(repo, dict) or repo.get("databaseId") != REPOSITORY_ID or
                repo.get("nameWithOwner") != REPOSITORY or repo.get("isPrivate") is not True or
                not isinstance(owner, dict) or owner.get("__typename") != "User" or owner.get("databaseId") != GITHUB_OWNER_ID):
            raise RequestRejected("comment_auth_mismatch")
        # GraphQL closes REST's same-second edit gap. Missing fields fail closed.
        # Receipts only reconcile exact bytes already durably saved locally.
        # Editing such a receipt cannot authorize or replay an adapter action.
        if not receipt and (comment.get("created_at") != comment.get("updated_at") or
                "lastEditedAt" not in node or node["lastEditedAt"] is not None or
                "editor" not in node or node["editor"] is not None or
                node.get("includesCreatedEdit") is not False or node.get("createdViaEmail") is not False):
            raise RequestRejected("edited_or_email_request")

    def publish(self, result):
        if not self.assert_channel():
            raise BridgeError("channel_closed")
        reply = self._call([COMMENT_ENDPOINT, "--method", "POST"], {"body": _json_bytes(result).decode()})
        if not _owned_comment(reply) or reply.get("body") != _json_bytes(result).decode():
            raise BridgeError("publication_unconfirmed")
        return reply["id"]


@contextmanager
def _lock(root):
    import fcntl
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise BridgeError("unsafe_state_path")
    fd = os.open(root / "lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("bridge_busy") from None
        yield
    finally:
        os.close(fd)


def _load(path):
    if path.is_symlink():
        raise BridgeError("unsafe_state_path")
    if not path.exists():
        if (path.parent / "initialized").exists():
            raise BridgeError("state_missing")
        return {"schema_version": 1, "scanned_at": 0, "records": {}}
    try:
        with path.open("rb") as handle:
            state = _parse(handle.read(MAX_STATE_BYTES + 1), MAX_STATE_BYTES)
        if (not isinstance(state, dict) or set(state) != {"schema_version", "scanned_at", "records"} or
                type(state["schema_version"]) is not int or state["schema_version"] != 1 or
                type(state["scanned_at"]) not in (int, float) or not math.isfinite(state["scanned_at"]) or state["scanned_at"] < 0 or
                not isinstance(state["records"], dict) or len(state["records"]) > MAX_RECORDS):
            raise ValueError
        required = {"comment_id", "body_sha256", "created_at", "started_at", "phase", "response", "published_comment_id", "publication_attempt_at"}
        for request_id, rec in state["records"].items():
            if (not _uuid4(request_id) or not isinstance(rec, dict) or set(rec) != required or
                    type(rec["comment_id"]) is not int or rec["comment_id"] <= 0 or
                    not isinstance(rec["body_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", rec["body_sha256"]) or
                    rec["phase"] not in {"started", "finished"} or
                    any(type(rec[key]) not in (int, float) or not math.isfinite(rec[key]) or rec[key] < 0
                        for key in ("created_at", "started_at", "publication_attempt_at")) or
                    (rec["published_comment_id"] is not None and (type(rec["published_comment_id"]) is not int or rec["published_comment_id"] <= 0))):
                raise ValueError
            if rec["phase"] == "started":
                if rec["response"] is not None or rec["published_comment_id"] is not None:
                    raise ValueError
            else:
                response = rec["response"]
                if (not isinstance(response, dict) or set(response) != RESULT_FIELDS or response["kind"] != "davos_result" or
                        response["schema_version"] != 1 or response["request_id"] != request_id or
                        response["request_comment_id"] != rec["comment_id"] or
                        response["state"] not in {"completed", "failed", "ambiguous", "rejected"} or
                        not isinstance(response["result"], dict) or len(_json_bytes(response)) > MAX_RESPONSE_BYTES):
                    raise ValueError
        return state
    except (BridgeError, ValueError, TypeError, OSError):
        raise BridgeError("state_corrupt") from None


def _sync_state_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _save(path, state):
    payload = _json_bytes(state)
    if len(payload) > MAX_STATE_BYTES or len(state["records"]) > MAX_RECORDS:
        raise BridgeError("state_capacity")
    _save_payload(path, payload)


def _save_payload(path, payload):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        marker = os.open(path.parent / "initialized", os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fsync(marker)
        finally:
            os.close(marker)
        _sync_state_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _channel_cursor_path(root):
    return Path(root) / f"scan-{ISSUE_ID}.json"


def _load_channel_binding(root):
    path = Path(root) / "channel.json"
    if path.is_symlink():
        raise BridgeError("unsafe_state_path")
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            value = _parse(handle.read(CHANNEL_STATE_MAX_BYTES + 1), CHANNEL_STATE_MAX_BYTES)
        expected = {"schema_version", "issue_number", "issue_id", "previous_issue_number", "previous_issue_id", "held_request_ids"}
        if (not isinstance(value, dict) or set(value) != expected or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                type(value["issue_number"]) is not int or value["issue_number"] != ISSUE_NUMBER or
                type(value["issue_id"]) is not int or value["issue_id"] != ISSUE_ID or
                type(value["previous_issue_number"]) is not int or value["previous_issue_number"] != PREVIOUS_ISSUE_NUMBER or
                type(value["previous_issue_id"]) is not int or value["previous_issue_id"] != PREVIOUS_ISSUE_ID or
                not isinstance(value["held_request_ids"], list) or len(value["held_request_ids"]) > MAX_RECORDS or
                any(not _uuid4(item) for item in value["held_request_ids"]) or
                value["held_request_ids"] != sorted(set(value["held_request_ids"]))):
            raise ValueError
        return value
    except (BridgeError, OSError, ValueError, TypeError):
        raise BridgeError("channel_binding_corrupt") from None


def _ensure_channel_binding(root, state):
    """Snapshot legacy IDs without editing, copying or replaying their records."""
    if not (Path(root) / "state.json").exists():
        _save(Path(root) / "state.json", state)
    value = _load_channel_binding(root)
    if value is not None:
        return set(value["held_request_ids"])
    if _channel_cursor_path(root).exists():
        raise BridgeError("channel_binding_missing")
    value = {"schema_version": 1, "issue_number": ISSUE_NUMBER, "issue_id": ISSUE_ID,
             "previous_issue_number": PREVIOUS_ISSUE_NUMBER, "previous_issue_id": PREVIOUS_ISSUE_ID,
             "held_request_ids": sorted(state["records"])}
    _save_payload(Path(root) / "channel.json", _json_bytes(value))
    # A new channel never reuses the old issue's discovery cursor. This durable
    # sidecar also makes a subsequently missing binding fail closed.
    _save_payload(_channel_cursor_path(root), _json_bytes({"schema_version": 1, "cursor": None}))
    return set(value["held_request_ids"])


class WorkBridge:
    def __init__(self, root, owner, *, transport=None, validate_action=None, execute_action=None, redactor=None, clock=time.time, revision="unknown"):
        self.root, self.owner = Path(root) / ".work_bridge", owner
        self.transport = transport if transport is not None else GitHubTransport(root)
        self.validate_action, self.execute_action = validate_action, execute_action
        self.redactor = redactor
        self.clock = clock
        self.revision = revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else "unknown"

    def _result(self, request_id, record, state, result):
        return {"schema_version": 1, "kind": "davos_result", "request_id": request_id,
                "request_comment_id": record["comment_id"], "state": state, "result": result,
                "runtime_revision": self.revision, "completed_at": _iso(self.clock())}

    def _actions(self):
        if self.validate_action is None or self.execute_action is None:
            from .work_actions import validate_action, execute_action
            from .permissions import redact_secret
            self.validate_action, self.execute_action = validate_action, execute_action
            self.redactor = redact_secret

    def _publish_pending(self, state, comments, *, held, publish=True):
        for request_id, record in state["records"].items():
            if request_id in held or record["phase"] != "finished" or record["published_comment_id"] is not None:
                continue
            body = _json_bytes(record["response"]).decode()
            existing = [row for row in comments if _owned_comment(row) and row.get("body") == body]
            for row in existing:
                try:
                    self.transport.authenticate(row, receipt=True)
                except RequestRejected:
                    # A matching but unverifiable receipt is not proof of
                    # absence. Keep the checkpoint behind it; never flood POSTs.
                    raise BridgeError("receipt_auth_unconfirmed") from None
                record["published_comment_id"] = row["id"]
                _save(self.root / "state.json", state)
                break
            if record["published_comment_id"] is not None:
                continue
            if not publish:
                continue
            # Once a POST may have happened, only exact authenticated readback
            # can settle it. Time passing, absence, restart or a closed issue
            # can never authorize a duplicate publication attempt.
            if record["publication_attempt_at"]:
                continue
            attempt_at = self.clock()
            if type(attempt_at) not in (int, float) or not math.isfinite(attempt_at) or attempt_at <= 0:
                raise BridgeError("invalid_publication_clock")
            record["publication_attempt_at"] = attempt_at
            _save(self.root / "state.json", state)
            try:
                record["published_comment_id"] = self.transport.publish(record["response"])
            except BridgeError as exc:
                code = str(exc) if re.fullmatch(r"[a-z_]{1,80}", str(exc)) else "publication_unconfirmed"
                logger.warning("Work bridge: result_publication_reconciliation_only code=%s", code)
                continue
            _save(self.root / "state.json", state)

    def poll(self):
        now = self.clock()
        try:
            if not isinstance(self.owner, str) or not self.owner.strip():
                raise BridgeError("owner_unconfigured")
            with _lock(self.root):
                path = self.root / "state.json"
                state = _load(path)
                channel_open = self.transport.assert_channel()
                held = _ensure_channel_binding(self.root, state)
                cursor_path = _channel_cursor_path(self.root)
                cursor = _load_cursor(cursor_path)
                # Results and expired idempotency records do not execute actions.
                for request_id, record in list(state["records"].items()):
                    if request_id in held:
                        continue  # Retained legacy records remain byte-for-byte values.
                    if record["phase"] == "started":
                        record["phase"] = "finished"
                        record["response"] = self._result(request_id, record, "ambiguous", {"error": "execution_interrupted"})
                        _save(path, state)
                    if record["published_comment_id"] is not None and now - record["created_at"] > RETENTION_SECONDS:
                        del state["records"][request_id]
                actions_this_poll = 0
                caught_up = False
                for comments, complete in self.transport.comment_pages(cursor):
                    self._publish_pending(state, comments, held=held, publish=False)
                    limited = False
                    for comment in comments:
                        if not channel_open:
                            cursor = comment["scan_cursor"]
                            continue
                        previous_cursor = cursor
                        cursor = comment["scan_cursor"]
                        if not _owned_comment(comment):
                            continue
                        try:
                            request = parse_request(comment.get("body"))
                        except BridgeError:
                            continue
                        # History discovery may restart when the sidecar is absent.
                        # A pruned, long-expired request must not create fresh receipts.
                        if self.clock() - _time(comment.get("created_at")) > RETENTION_SECONDS:
                            continue
                        request_id = request["request_id"]
                        digest = hashlib.sha256(_json_bytes(request)).hexdigest()
                        record = state["records"].get(request_id)
                        if record is not None:
                            if record["comment_id"] != comment["id"] or record["body_sha256"] != digest:
                                logger.warning("Work bridge: request_id_replay_rejected")
                            continue
                        try:
                            self.transport.authenticate(comment)
                        except RequestRejected as exc:
                            # Do not reflect unauthenticated payloads back into GitHub.
                            logger.warning("Work bridge: %s", str(exc))
                            continue
                        created = _time(comment.get("created_at"))
                        record = {"comment_id": comment["id"], "body_sha256": digest, "created_at": created,
                                  "started_at": now, "phase": "started", "response": None,
                                  "published_comment_id": None, "publication_attempt_at": 0}
                        rejected = None
                        current_now = self.clock()
                        if created > current_now + 30 or current_now - created > REQUEST_TTL:
                            rejected = "request_expired_or_future"
                        else:
                            self._actions()
                            try:
                                self.validate_action(request["action"], request["args"])
                            except ValueError as exc:
                                code = str(exc)
                                rejected = code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "invalid_action"
                        state["records"][request_id] = record
                        if rejected:
                            record["phase"] = "finished"
                            record["response"] = self._result(request_id, record, "rejected", {"error": rejected})
                            _save(path, state)
                            continue
                        if actions_this_poll >= MAX_ACTIONS_PER_POLL:
                            del state["records"][request_id]
                            cursor = previous_cursor
                            limited = True
                            break  # Resume at this unprocessed edge on the next poll.
                        if self.clock() - created > REQUEST_TTL:
                            record["phase"] = "finished"
                            record["response"] = self._result(request_id, record, "rejected", {"error": "request_expired_or_future"})
                            _save(path, state)
                            continue
                        _save(path, state)  # Must durably succeed before any adapter action.
                        actions_this_poll += 1
                        try:
                            from .work_image_receipts import request_scope
                            with request_scope(request_id, record["comment_id"], self.owner, self.root, self.revision):
                                result = safe_result(self.execute_action(request["action"], request["args"], owner=self.owner), self.redactor)
                            evidence = result.get("evidence")
                            outcome = ("ambiguous" if isinstance(evidence, dict) and evidence.get("ambiguous") is True else
                                       "failed" if result.get("status") == "error" else "completed")
                        except Exception:
                            outcome, result = "ambiguous", {"error": "adapter_execution_unconfirmed"}
                        record["phase"] = "finished"
                        record["response"] = self._result(request_id, record, outcome, result)
                        _save(path, state)
                    state["scanned_at"] = now
                    _save(path, state)  # Journal durability must precede discovery progress.
                    # Closing the issue pauses execution, not the queue itself.
                    # Keep its durable position so a still-fresh request can run
                    # after reopening; the journal deduplicates reconciliation.
                    if channel_open:
                        _save_payload(cursor_path, _json_bytes({"schema_version": 1, "cursor": cursor}))
                    if limited:
                        break
                    if complete:
                        caught_up = True
                        break
                if caught_up:
                    self._publish_pending(state, [], held=held, publish=channel_open)
                return {"state": "active" if channel_open else "paused", "records": len(state["records"]),
                        "scan_pending": not caught_up, "held_legacy_records": len(held),
                        "unpublished": sum(rec["published_comment_id"] is None for rec in state["records"].values())}
        except BridgeError as exc:
            logger.warning("Work bridge: %s", str(exc))
            return {"state": "error", "error": str(exc)}
        except Exception:
            logger.warning("Work bridge: internal_error")
            return {"state": "error", "error": "internal_error"}


def start_work_bridge():
    """Start one Mac daemon. Merely importing this module does not load .env."""
    global _thread
    if sys.platform != "darwin":
        return None
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        from .config import OWNER_ID, PROJECT_ROOT
        if not OWNER_ID:
            logger.warning("Work bridge: owner_unconfigured")
            return None
        revision = "unknown"
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                                    text=True, timeout=5, shell=False, check=True)
            revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        bridge = WorkBridge(PROJECT_ROOT, OWNER_ID, revision=revision)
        def run():
            while True:
                bridge.poll()
                time.sleep(POLL_SECONDS)
        _thread = threading.Thread(target=run, name="work-bridge", daemon=True)
        _thread.start()
        return _thread
