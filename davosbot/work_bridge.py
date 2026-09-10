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
ISSUE_NUMBER = 64
ISSUE_ID = 5354864924
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
MAX_PAGES = 20
PAGE_SIZE = 50
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


def safe_result(value, redactor=None):
    """Bound and redact adapter output before it reaches disk or GitHub."""
    sensitive = re.compile(r"password|secret|token|credential|api_key|authorization|cookie|private_key", re.I)
    tokens = re.compile(r"(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~-]+", re.I)
    def clean(item, depth=0):
        if depth > 12:  # Full capability JSON schemas include nested array objects.
            raise ValueError
        if isinstance(item, dict):
            if len(item) > 100 or any(not isinstance(key, str) or len(key) > 100 for key in item):
                raise ValueError
            return {key: "[redacted]" if sensitive.search(key) else clean(val, depth + 1) for key, val in item.items()}
        if isinstance(item, list):
            if len(item) > 100:
                raise ValueError
            return [clean(val, depth + 1) for val in item]
        if isinstance(item, str):
            text = redactor(item) if redactor is not None else item
            text = tokens.sub("[redacted]", text)
            text = re.sub(r"-----BEGIN[^\n]*PRIVATE KEY-----[\s\S]*", "[redacted]", text)
            return text
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

    def comments(self, since):
        """Fetch every page before advancing state; an overfull scan fails closed."""
        from urllib.parse import quote
        result, seen = [], set()
        timestamp = quote(_iso(since), safe="")
        for page in range(1, MAX_PAGES + 1):
            rows = self._call([f"{COMMENT_ENDPOINT}?per_page={PAGE_SIZE}&page={page}&since={timestamp}", "--method", "GET"])
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise BridgeError("invalid_comments_page")
            for row in rows:
                if not isinstance(row, dict) or type(row.get("id")) is not int:
                    raise BridgeError("invalid_comments_page")
                if row["id"] not in seen:
                    result.append(row)
                    seen.add(row["id"])
            if len(rows) < PAGE_SIZE:
                return sorted(result, key=lambda row: row["id"])
        raise BridgeError("comments_scan_capacity")

    def authenticate(self, comment):
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
        if (comment.get("created_at") != comment.get("updated_at") or
                "lastEditedAt" not in node or node["lastEditedAt"] is not None or
                "editor" not in node or node["editor"] is not None or
                node.get("includesCreatedEdit") is not False or node.get("createdViaEmail") is not False):
            raise RequestRejected("edited_or_email_request")

    def publish(self, result):
        self.assert_channel()
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

    def _publish_pending(self, state, comments):
        for request_id, record in state["records"].items():
            if record["phase"] != "finished" or record["published_comment_id"] is not None:
                continue
            body = _json_bytes(record["response"]).decode()
            existing = [row for row in comments if _owned_comment(row) and row.get("body") == body]
            for row in existing:
                try:
                    self.transport.authenticate(row)
                except RequestRejected:
                    continue
                record["published_comment_id"] = row["id"]
                _save(self.root / "state.json", state)
                break
            if record["published_comment_id"] is not None:
                continue
            if record["publication_attempt_at"] and self.clock() - record["publication_attempt_at"] < RESULT_RETRY_SECONDS:
                continue
            record["publication_attempt_at"] = self.clock()
            _save(self.root / "state.json", state)
            try:
                record["published_comment_id"] = self.transport.publish(record["response"])
            except BridgeError:
                logger.warning("Work bridge: result_publication_pending")
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
                pending = [rec["created_at"] for rec in state["records"].values() if rec["published_comment_id"] is None]
                since = min([now - REQUEST_TTL - 60, state["scanned_at"] or now] + pending) - 60
                comments = self.transport.comments(max(0, since))
                # Results and expired idempotency records do not execute actions.
                for request_id, record in list(state["records"].items()):
                    if record["phase"] == "started":
                        record["phase"] = "finished"
                        record["response"] = self._result(request_id, record, "ambiguous", {"error": "execution_interrupted"})
                        _save(path, state)
                    if record["published_comment_id"] is not None and now - record["created_at"] > RETENTION_SECONDS:
                        del state["records"][request_id]
                self._publish_pending(state, comments)
                if not channel_open:
                    state["scanned_at"] = now
                    _save(path, state)
                    return {"state": "paused", "records": len(state["records"])}
                actions_this_poll = 0
                for comment in comments:
                    if not _owned_comment(comment):
                        continue
                    try:
                        request = parse_request(comment.get("body"))
                    except BridgeError:
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
                        break  # Overlap scan includes this still-unprocessed comment next time.
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
                _save(path, state)
                self._publish_pending(state, comments)
                return {"state": "active", "records": len(state["records"]),
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
