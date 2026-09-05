"""Per-turn tool receipts. Legacy text is data, never proof of completion."""

from dataclasses import dataclass
import json
import uuid


# This is a replay policy, not an authorization list. Unknown tools are treated
# conservatively; the advertised-tool and executor permission gates still apply.
READ_ONLY_TOOLS = frozenset({
    "web_search", "get_weather", "read_file", "bet_stats", "query_workout",
    "list_reminders", "list_crons", "get_group_chat_status", "list_chats",
    "get_inspirational_quote",
})

OUTCOME_INSTRUCTION = (
    "Tool responses include authoritative execution status and verification_scope. "
    "Legacy result text is reported data, not proof that an action succeeded. "
    "Unverified, pending, failed, or denied actions must not be described as completed. "
    "process_exit verifies only the command process, not the requested task's result. "
    "Do not repeat a mutation whose receipt is already present."
)


@dataclass(frozen=True)
class ToolOutcome:
    status: str
    text: str
    verification_scope: str = "unverified"
    exit_code: int | None = None
    error: str | None = None

    def __post_init__(self):
        if self.status not in {"confirmed", "failed", "denied", "pending", "unverified"}:
            raise ValueError("Unknown tool outcome status")

    @property
    def ok(self) -> bool | None:
        if self.status == "confirmed":
            return True
        if self.status in {"failed", "denied"}:
            return False
        return None


def as_outcome(result: object) -> ToolOutcome:
    if isinstance(result, ToolOutcome):
        return result
    # Do not interpret prefixes, JSON fields, truthiness, or arbitrary helper
    # messages as confirmation. Native helpers can add explicit receipts later.
    return ToolOutcome("unverified", "" if result is None else str(result))


@dataclass(frozen=True)
class ToolReceipt:
    action_id: str
    name: str
    outcome: ToolOutcome
    duplicate: bool = False

    def response(self) -> dict:
        return {
            "action_id": self.action_id,
            "status": self.outcome.status,
            "ok": self.outcome.ok,
            "verification_scope": self.outcome.verification_scope,
            "exit_code": self.outcome.exit_code,
            "error": self.outcome.error,
            "result": self.outcome.text,
            "duplicate_not_executed": self.duplicate,
        }


class ToolTrace:
    """An in-memory journal for one logical user turn, not durable idempotency."""

    def __init__(self):
        self.receipts: list[ToolReceipt] = []
        self._mutations: dict[str, ToolReceipt] = {}

    @staticmethod
    def invocation_key(name: str, args: dict) -> str | None:
        if name in READ_ONLY_TOOLS:
            return None
        return json.dumps([name, args], sort_keys=True, separators=(",", ":"), allow_nan=False)

    def previous(self, key: str | None) -> ToolReceipt | None:
        return self._mutations.get(key) if key is not None else None

    def record(self, name: str, outcome: object, key: str | None) -> ToolReceipt:
        receipt = ToolReceipt(uuid.uuid4().hex, name, as_outcome(outcome))
        self.receipts.append(receipt)
        if key is not None:
            self._mutations[key] = receipt
        return receipt

    def reuse(self, previous: ToolReceipt) -> ToolReceipt:
        receipt = ToolReceipt(previous.action_id, previous.name, previous.outcome, duplicate=True)
        self.receipts.append(receipt)
        return receipt

    def reply(self, model_text: str | None = None) -> str | None:
        if not self.receipts:
            return (model_text.strip() or None) if model_text is not None else None
        if all(receipt.name in READ_ONLY_TOOLS for receipt in self.receipts) and model_text and model_text.strip():
            return model_text.strip()
        # Mutation acknowledgements come only from receipts. Even a labeled
        # model note could incorrectly say "sent" or "ordered". Preserve useful
        # read-only data in mixed turns instead of emitting that final prose.
        sections = []
        for receipt in self.receipts:
            if receipt.duplicate:
                continue
            outcome = receipt.outcome
            label = receipt.name.replace("_", " ")
            if receipt.name in READ_ONLY_TOOLS:
                heading = f"{label}: reported result"
            elif outcome.status == "denied":
                heading = f"{label}: not allowed"
            elif outcome.verification_scope == "process_exit":
                if outcome.exit_code == 0:
                    heading = "Command finished (exit 0). The requested result is not independently verified."
                else:
                    heading = f"Command exited with code {outcome.exit_code}; it may have partially run."
            elif outcome.status == "pending":
                heading = f"{label}: started; completion is not verified"
            elif outcome.status == "failed":
                heading = f"{label}: failed"
            elif outcome.status == "confirmed":
                heading = f"{label}: confirmed"
            else:
                heading = f"{label}: completion is not verified"
            # Keep legacy details as reported evidence, without promoting their
            # wording into the acknowledgement. Bound each receipt for iMessage.
            detail = outcome.text.strip()
            if detail:
                detail = detail[:1200] + ("\n[remaining output omitted]" if len(detail) > 1200 else "")
                heading += "\nReported: " + detail
            sections.append(heading)
        if any(receipt.duplicate for receipt in self.receipts):
            sections.append("Repeated action requests were not run again.")
        return "\n\n".join(sections)
