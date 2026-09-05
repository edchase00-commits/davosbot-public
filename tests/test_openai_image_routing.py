import ast
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from davosbot import failure_copy
from davosbot.openai_images import parse_openai_image_intent


ROOT = Path(__file__).resolve().parents[1]


class _TraceStub:
    route = "unknown"

    def __init__(self, **_kwargs):
        pass

    def flag(self, _name):
        pass

    def set_route(self, route):
        self.route = route


def _load_openai_image_handler(namespace_overrides):
    return _load_main_function(
        "_handle_openai_image_intent",
        namespace_overrides,
        dependencies=(
            "_image_context_key",
            "_image_route_key_from_text",
            "_project_relative_path",
            "_generated_image_dirs",
            "_image_path_matches_route",
            "_latest_generated_image_path",
            "_recent_generated_image_paths",
            "_remember_generated_image",
            "_active_image_jobs_for_context",
            "_generated_image_queue_items",
            "_format_image_job_line",
            "_handle_generated_image_queue_request",
            "_handle_last_generated_image_request",
            "_image_provider_override_from_text",
        ),
    )


def _load_image_capability_handler(namespace_overrides):
    return _load_main_function("_handle_image_capability_status", namespace_overrides)


def _load_image_job_runner(namespace_overrides):
    return _load_main_function(
        "_run_image_generation_job",
        namespace_overrides,
        dependencies=("_compose_reference_generation_prompt", "_finish_image_job"),
    )


def _load_main_function(function_name, namespace_overrides, dependencies=()):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    wanted = set(dependencies) | {function_name}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "_MessageTrace": _TraceStub,
        "_trace_call": lambda _trace, _phase, fn, *args, **kwargs: fn(*args, **kwargs),
        "_log_message_trace": lambda *_args, **_kwargs: None,
        "_log_quality_signal": lambda *_args, **_kwargs: None,
        "SLOW_MESSAGE_LOG_SECONDS": 999999,
        "OPENAI_IMAGE_GENERATION_TOOL": "openai_image_generation",
        "OPENAI_IMAGE_SCAN_TOOL": "openai_image_scan",
        "os": os,
        "Path": Path,
        "re": re,
        "__file__": str(ROOT / "davosbot" / "main.py"),
        "__name__": "davosbot.main",
        "__package__": "davosbot",
        "PROJECT_ROOT": ROOT,
        "GENERATED_DIR": str(ROOT / "generated"),
        "IMAGE_OUTPUT_DIR": str(ROOT / "generated" / "images"),
        "_LAST_GENERATED_IMAGE_RE": re.compile(
            r"^\s*(?:(?:can|could|would)\s+(?:you|u)\s+)?"
            r"(?:show|send|resend|share|fetch|pull\s+up)\s+(?:me\s+)?"
            r"(?:the\s+)?(?:last\s+|latest\s+|recent\s+|generated\s+)?"
            r"(?:image|picture|photo|generation)\b"
            r"|^\s*(?:where\s+is|what\s+happened\s+to)\s+(?:the\s+)?"
            r"(?:last\s+|latest\s+|recent\s+|generated\s+)?(?:image|picture|photo)\b",
            re.IGNORECASE,
        ),
        "_GENERATED_IMAGE_CACHE": {},
        "_ACTIVE_IMAGE_JOBS": {},
        "_IMAGE_JOB_LOCK": threading.RLock(),
        "_GENERATED_IMAGE_EXTENSIONS": {".png", ".jpg", ".jpeg", ".webp"},
        "_GENERATED_IMAGE_QUEUE_LIMIT": 5,
        "_IMAGE_QUEUE_STATUS_RE": re.compile(
            r"^\s*nano\s*banana\b.{0,80}\b(?:queue|status|history|list|where\s+is|what\s+happened)\b"
            r"|^\s*(?:what(?:'s|\s+is)\s+)?(?:in\s+)?(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|status|history|list)\b"
            r"|^\s*(?:list|show)\s+(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|history)\b"
            r"|^\s*how\s+many\s+(?:images?|pictures?|photos?|generated\s+images?)\s+(?:are\s+)?(?:queued|saved|recent|in\s+(?:the\s+)?queue)\b"
            r"|^\s*(?:queued\s+images?|image\s+queue|queue\s+images?|queue\s+image)\b"
            r"|^\s*(?:where\s+is|what\s+happened\s+to)\s+(?:my\s+|the\s+)?(?:generated\s+)?(?:image|picture|photo)\b"
            r"|^\s*(?:my\s+|the\s+)?(?:image|picture|photo|generation)\b.{0,80}\b(?:never|not|didn'?t|doesn'?t|failed|missing|isn'?t)\b.{0,80}\b(?:generated|generate|sent|send|come\s+through|show\s+up|arrive|there)\b"
            r"|^\s*(?:no|still\s+no)\s+(?:generated\s+)?(?:image|picture|photo)\b",
            re.IGNORECASE,
        ),
        "_NANO_BANANA_RE": re.compile(r"\bnano\s*banana\b", re.IGNORECASE),
        "_GEMINI_IMAGE_PROVIDER_HINT_RE": re.compile(
            r"\b(?:using|via|with|through|on)\s+(?:google\s+)?gemini\b"
            r"|^\s*@?\s*davos(?:bot)?\b[\s,;:.-]*(?:google\s+)?gemini\b.{0,100}\b"
            r"(?:image\s*(?:gen|generate)|generate|create|make|draw|render)\b"
            r"|^\s*(?:google\s+)?gemini\b.{0,100}\b"
            r"(?:image\s*(?:gen|generate)|generate|create|make|draw|render)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "_IMAGE_QUEUE_SEND_RE": re.compile(
            r"^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?nano\s*banana\b.{0,80}\b(?:image|queue|history)\b"
            r"|^\s*nano\s*banana\b.{0,80}\b(?:send|resend|share)\b"
            r"|^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?(?:image|generated\s+image)\s+(?:queue|history)\b"
            r"|^\s*(?:send|resend|share)\s+(?:all\s+)?(?:queued|recent)\s+(?:images?|pictures?|photos?)\b"
            r"|^\s*(?:send|resend|share)\s+(?:me\s+)?(?:the\s+)?(?:queued\s+)?(?:image|picture|photo)\b",
            re.IGNORECASE,
        ),
        "_IMAGE_CAPABILITY_STATUS_RE": re.compile(
            r"^\s*(?:image|images|vision|gpt\s+scan|image\s+gen(?:eration)?)\s+"
            r"(?:routing|route|status|provider|providers|model|models|config|configuration)\b"
            r"|^\s*(?:what|which|show|tell\s+me|status)\b.{0,80}\b(?:image|vision|scan|generation)\b"
            r".{0,80}\b(?:model|provider|route|routing|configured|config|status)\b"
            r"|\b(?:image|vision|scan|generation)\s+(?:model|provider|route|routing|config|status)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "_IMAGE_SCAN_CAPABILITY_RE": re.compile(
            r"\b(?:can|could|do|does|will)\b.{0,60}\b(?:you|davos|davosbot)\b"
            r".{0,100}\b(?:scan|read|analy[sz]e|describe|inspect|look\s+at|view)\b"
            r".{0,100}\b(?:images?|photos?|pictures?|screenshots?|attachments?)\b"
            r"|\b(?:images?|photos?|pictures?|screenshots?|attachments?)\s+scans?\b"
            r".{0,100}\b(?:work|works|available|supported|capab|gcs?|group\s+chats?|groups?)\b"
            r"|\b(?:scan|read|analy[sz]e|describe|inspect|look\s+at|view)\b"
            r".{0,100}\b(?:images?|photos?|pictures?|screenshots?|attachments?)\b"
            r".{0,100}\b(?:work|works|available|supported|capab|gcs?|group\s+chats?|groups?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "_IMAGE_GENERATION_CAPABILITY_RE": re.compile(
            r"\b(?:can|could|do|does|will)\b.{0,60}\b(?:you|davos|davosbot)\b"
            r".{0,100}\b(?:generate|create|make|draw|render)\b"
            r".{0,100}\b(?:images?|photos?|pictures?|graphics?|logos?|art|memes?)\b"
            r"|\b(?:image|photo|picture|logo|art|meme)\s+gen(?:eration)?\b"
            r".{0,100}\b(?:work|works|available|supported|capab|enabled|configured)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "_SCREENSHOT_ISSUE_LOG_RE": re.compile(
            r"\b(?:log|record|capture)\b.{0,80}\b(?:screenshot|image|photo|picture)\b"
            r".{0,80}\b(?:issue|bug|error|failure|glitch|wrong|bad|broken)\b"
            r"|\b(?:log|record|capture)\b.{0,80}\b(?:what\s+went\s+wrong|expected\s+vs\s+actual)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "time": SimpleNamespace(time=lambda: 123.0),
        "is_owner": lambda sender: False,
        "is_admin": lambda sender: False,
        "is_approved_user": lambda sender: False,
        "is_imessage_reaction": lambda text, associated_message_type=None, associated_message_guid=None: False,
        "image_access_denial": lambda sender: None,
        "choose_scan_provider": lambda: "gemini",
        "find_recent_image_attachment": lambda recipient, sender=None: None,
        "validate_image_path": lambda image_path: (True, "", "image/png"),
        "_start_image_generation_job": (
            lambda sender, prompt, image_path, recipient, is_group=False, route_key="", provider_override="":
                "On it, generating image. Estimate: about 2-4 minutes."
        ),
        "logger": SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
        "redact_secret": lambda text: text,
        "_failure_copy": failure_copy,
    }
    namespace.update(namespace_overrides)
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace[function_name]


class OpenAIImageRoutingTests(unittest.TestCase):
    def test_attached_image_opinion_requests_are_concise(self):
        intent = parse_openai_image_intent("what do u think of this guy", has_image=True)

        self.assertIsNotNone(intent)
        self.assertEqual("scan", intent.kind)
        self.assertIn("1-2 short sentences", intent.prompt)
        self.assertIn("skip the full image audit", intent.prompt)
        self.assertIn("what do u think of this guy", intent.prompt)

    def test_image_scan_roast_can_request_atl_style(self):
        for has_image in (False, True):
            with self.subTest(has_image=has_image):
                intent = parse_openai_image_intent("image scan and roast in atl", has_image=has_image)

                self.assertIsNotNone(intent)
                self.assertEqual("scan", intent.kind)
                self.assertIn("Roast this image in 1-3 short lines", intent.prompt)
                self.assertIn("ATL/Atlanta persona style", intent.prompt)

    def test_attached_roast_stays_short(self):
        intent = parse_openai_image_intent("roast", has_image=True)

        self.assertIsNotNone(intent)
        self.assertEqual("scan", intent.kind)
        self.assertIn("1-3 short lines", intent.prompt)
        self.assertIn("No disclaimers", intent.prompt)

    def test_unapproved_explicit_image_request_is_denied_before_api(self):
        calls = []
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat"),
            "is_owner": lambda sender: False,
            "is_admin": lambda sender: False,
            "is_approved_user": lambda sender: False,
            "get_tool_uses_today": lambda sender, tool: 0,
            "generate_image": lambda prompt: calls.append(prompt),
            "scan_image": lambda image_path, prompt: calls.append(prompt),
            "log_tool_use": lambda sender, tool: calls.append(tool),
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: calls.append(text),
            "choose_generation_provider": lambda: "gemini",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("friend", "image gen cat", None, "friend")

        self.assertIn("approved users only", reply)
        self.assertEqual([], calls)

    def test_image_access_limit_blocks_api_call(self):
        calls = []
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat"),
            "is_owner": lambda sender: False,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_access_denial": lambda sender: "Image limit reached (5/5 today). the owner can extend you by 5 more.",
            "get_tool_uses_today": lambda sender, tool: 3,
            "generate_image": lambda prompt: calls.append(prompt),
            "scan_image": lambda image_path, prompt: calls.append(prompt),
            "log_tool_use": lambda sender, tool: calls.append(tool),
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: calls.append(text),
            "choose_generation_provider": lambda: "gemini",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("admin", "image gen cat", None, "chat")

        self.assertIn("Image limit reached", reply)
        self.assertEqual([], calls)

    def test_generation_sends_file_and_logs_openai_usage(self):
        logged = []
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            image_path = str(Path(tmp) / "image.png")
            Path(image_path).write_bytes(b"png")
            handler = _load_openai_image_handler({
                "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat"),
                "is_owner": lambda sender: False,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "get_tool_uses_today": lambda sender, tool: 0,
                "generate_image": lambda prompt: SimpleNamespace(ok=True, message="ok", path=image_path, api_called=True, provider="gemini"),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: logged.append((sender, tool)),
                "send_file": lambda recipient, path, is_group=False: sent.append((recipient, path, is_group)) or True,
                "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
                "choose_generation_provider": lambda: "gemini",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })

            reply = handler("admin", "image gen cat", None, "chat-id", is_group=True)

        self.assertIn("On it, generating image", reply)
        self.assertEqual([("admin", "openai_image_generation")], logged)
        self.assertEqual([], sent)

    def test_owner_generation_is_uncapped_and_logs_api_usage_only(self):
        logged = []
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            image_path = str(Path(tmp) / "image.png")
            Path(image_path).write_bytes(b"png")
            handler = _load_openai_image_handler({
                "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat"),
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "get_tool_uses_today": lambda sender, tool: 999,
                "generate_image": lambda prompt: SimpleNamespace(ok=True, message="ok", path=image_path, api_called=True, provider="gemini"),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: logged.append((sender, tool)),
                "send_file": lambda recipient, path, is_group=False: sent.append((recipient, path, is_group)) or True,
                "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
                "choose_generation_provider": lambda: "gemini",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })

            reply = handler("owner", "image gen cat", None, "owner")

        self.assertIn("On it, generating image", reply)
        self.assertEqual([], logged)

    def test_last_generated_image_request_resends_cached_file(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            image_path = str(Path(tmp) / "image.png")
            Path(image_path).write_bytes(b"png")

            def parse_intent(text, has_image):
                if text.startswith("image gen"):
                    return SimpleNamespace(kind="generate", prompt="cat")
                return None

            handler = _load_openai_image_handler({
                "parse_openai_image_intent": parse_intent,
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "generate_image": lambda prompt: SimpleNamespace(ok=True, message="ok", path=image_path, api_called=True, provider="local"),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: None,
                "send_file": lambda recipient, path, is_group=False: sent.append((recipient, path, is_group)) or True,
                "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
                "choose_generation_provider": lambda: "local",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 2-4 minutes",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })

            # Seed cache directly because generation now runs in a background worker.
            namespace_cache = handler.__globals__["_GENERATED_IMAGE_CACHE"]
            namespace_cache["dm:owner"] = {
                "path": image_path,
                "provider": "local",
                "ts": 123.0,
                "items": [{"path": image_path, "provider": "local", "ts": 123.0}],
            }
            second = handler("owner", "can u show me the image", None, "owner")

        self.assertIn("Sent the last generated image", second)
        self.assertEqual(("owner", image_path, False), sent[-1])

    def test_image_queue_status_and_send_are_chat_scoped(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            first_path = str(Path(tmp) / "first.png")
            second_path = str(Path(tmp) / "second.png")
            Path(first_path).write_bytes(b"png1")
            Path(second_path).write_bytes(b"png2")

            def parse_intent(text, has_image):
                if text.startswith("image gen first"):
                    return SimpleNamespace(kind="generate", prompt="first")
                if text.startswith("image gen second"):
                    return SimpleNamespace(kind="generate", prompt="second")
                return None

            paths = {"first": first_path, "second": second_path}
            handler = _load_openai_image_handler({
                "parse_openai_image_intent": parse_intent,
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "generate_image": lambda prompt: SimpleNamespace(ok=True, message="ok", path=paths[prompt], api_called=True, provider="local"),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: None,
                "send_file": lambda recipient, path, is_group=False: sent.append((recipient, path, is_group)) or True,
                "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
                "choose_generation_provider": lambda: "local",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 2-4 minutes",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })

            handler.__globals__["_GENERATED_IMAGE_CACHE"]["group:chat-id"] = {
                "path": second_path,
                "provider": "local",
                "ts": 124.0,
                "items": [
                    {"path": second_path, "provider": "local", "ts": 124.0},
                    {"path": first_path, "provider": "local", "ts": 123.0},
                ],
            }
            status = handler("owner", "image queue", None, "chat-id", is_group=True)
            send_reply = handler("owner", "send image queue", None, "chat-id", is_group=True)

        self.assertIn("Recent generated images", status)
        self.assertIn("second.png", status)
        self.assertIn("first.png", status)
        self.assertNotIn("via local", status)
        self.assertIn("Sent 2 recent generated image(s).", send_reply)
        self.assertIn(("chat-id", second_path, True), sent)
        self.assertIn(("chat-id", first_path, True), sent)

    def test_active_image_job_line_omits_provider(self):
        formatter = _load_main_function("_format_image_job_line", {})

        reply = formatter({"provider": "gemini", "started_ts": 120.0})

        self.assertIn("1 active image job", reply)
        self.assertIn("elapsed 3s", reply)
        self.assertNotIn("gemini", reply.lower())

    def test_natural_image_queue_phrases_are_deterministic(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            image_path = str(Path(tmp) / "fish.png")
            Path(image_path).write_bytes(b"png")

            def parse_intent(text, has_image):
                if text.startswith("image gen"):
                    return SimpleNamespace(kind="generate", prompt="fish")
                return None

            handler = _load_openai_image_handler({
                "parse_openai_image_intent": parse_intent,
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "generate_image": lambda prompt: SimpleNamespace(ok=True, message="ok", path=image_path, api_called=True, provider="local"),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: None,
                "send_file": lambda recipient, path, is_group=False: sent.append((recipient, path, is_group)) or True,
                "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
                "choose_generation_provider": lambda: "local",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 2-4 minutes",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })

            handler.__globals__["_GENERATED_IMAGE_CACHE"]["dm:owner"] = {
                "path": image_path,
                "provider": "local",
                "ts": 123.0,
                "items": [{"path": image_path, "provider": "local", "ts": 123.0}],
            }
            count_reply = handler("owner", "How many images are queued", None, "owner")
            queue_reply = handler("owner", "Queue image", None, "owner")
            send_reply = handler("owner", "send me the queued image", None, "owner")

        self.assertIn("Recent generated images (1):", count_reply)
        self.assertIn("fish.png", count_reply)
        self.assertIn("Recent generated images (1):", queue_reply)
        self.assertIn("Sent 1 recent generated image(s).", send_reply)

    def test_image_generation_failure_complaint_checks_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = str(Path(tmp) / "fish.png")
            Path(image_path).write_bytes(b"png")

            handler = _load_openai_image_handler({
                "parse_openai_image_intent": lambda text, has_image: None,
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "generate_image": lambda prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
                "log_tool_use": lambda sender, tool: None,
                "send_file": lambda recipient, path, is_group=False: True,
                "send_message": lambda recipient, text, is_group=False: None,
                "choose_generation_provider": lambda: "local",
                "choose_scan_provider": lambda: "gemini",
                "estimate_generation_time": lambda provider=None: "about 2-4 minutes",
                "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
                "image_provider_status": lambda: "Image routing",
            })
            handler.__globals__["_GENERATED_IMAGE_CACHE"]["dm:owner"] = {
                "path": image_path,
                "provider": "local",
                "ts": 123.0,
                "items": [{"path": image_path, "provider": "local", "ts": 123.0}],
            }

            reply = handler("owner", "my image was never generated", None, "owner")

        self.assertIn("Recent generated images (1):", reply)
        self.assertIn("fish.png", reply)

    def test_nano_banana_generation_uses_separate_route_key(self):
        starts = []

        def start_job(sender, prompt, image_path, recipient, is_group=False, route_key="", provider_override=""):
            starts.append((prompt, route_key, provider_override, recipient, is_group))
            return "On it, generating image. Estimate: about 20-60 seconds."

        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat logo"),
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_access_denial": lambda sender: None,
            "_start_image_generation_job": start_job,
        })

        reply = handler("owner", "nano banana image of a cat logo", None, "owner")

        self.assertIn("On it, generating image", reply)
        self.assertEqual([("cat logo", "nano_banana", "gemini", "owner", False)], starts)

    def test_direct_addressed_gemini_generation_uses_gemini_override(self):
        starts = []

        def start_job(sender, prompt, image_path, recipient, is_group=False, route_key="", provider_override=""):
            starts.append((prompt, route_key, provider_override, recipient, is_group))
            return "On it, generating image. Estimate: about 20-60 seconds."

        handler = _load_openai_image_handler({
            "parse_openai_image_intent": parse_openai_image_intent,
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_access_denial": lambda sender: None,
            "_start_image_generation_job": start_job,
        })

        reply = handler("owner", "Davos image gen a fish using Gemini", None, "owner")

        self.assertIn("On it, generating image", reply)
        self.assertEqual([("a fish", "", "gemini", "owner", False)], starts)

    def test_background_job_honors_gemini_provider_override(self):
        calls = []
        image_path = "/tmp/generated-gemini.png"

        runner = _load_image_job_runner({
            "_ACTIVE_IMAGE_JOBS": {"dm:owner": {"job_id": "job-1"}},
            "generate_gemini_image": lambda prompt: calls.append(("gemini", prompt)) or SimpleNamespace(
                ok=True,
                path=image_path,
                api_called=True,
                provider="gemini",
            ),
            "generate_image": lambda prompt: calls.append(("default", prompt)) or SimpleNamespace(
                ok=False,
                path=None,
                api_called=True,
                provider="local",
                message="wrong route",
            ),
            "generate_local_image": lambda prompt: calls.append(("local", prompt)),
            "generate_openai_image": lambda prompt: calls.append(("openai", prompt)),
            "generate_nano_banana_image": lambda prompt: calls.append(("nano", prompt)),
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: calls.append(("message", text)),
            "_remember_generated_image": lambda sender, recipient, is_group, path, provider, route_key="": calls.append(
                ("remember", path, provider, route_key)
            ),
            "log_tool_use": lambda sender, tool: calls.append(("log", tool)),
            "is_owner": lambda sender: True,
            "redact_secret": lambda text: text,
            "logger": SimpleNamespace(
                warning=lambda *args, **kwargs: calls.append(("warning", args)),
                exception=lambda *args, **kwargs: calls.append(("exception", args)),
            ),
        })

        runner({
            "key": "dm:owner",
            "job_id": "job-1",
            "sender": "owner",
            "recipient": "owner",
            "is_group": False,
            "prompt": "a fish",
            "provider_override": "gemini",
            "route_key": "",
        })

        self.assertIn(("gemini", "a fish"), calls)
        self.assertNotIn(("default", "a fish"), calls)
        self.assertIn(("remember", image_path, "gemini", ""), calls)

    def test_nano_banana_queue_is_separate_from_default_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_path = str(Path(tmp) / "local_image_20260606.png")
            nano_path = str(Path(tmp) / "nano_banana_image_20260606.png")
            Path(default_path).write_bytes(b"default")
            Path(nano_path).write_bytes(b"nano")

            handler = _load_openai_image_handler({
                "parse_openai_image_intent": lambda text, has_image: None,
                "is_owner": lambda sender: True,
                "is_admin": lambda sender: True,
                "is_approved_user": lambda sender: True,
                "image_access_denial": lambda sender: None,
                "choose_generation_provider": lambda: "local",
                "choose_scan_provider": lambda: "gemini",
            })
            cache = handler.__globals__["_GENERATED_IMAGE_CACHE"]
            cache["dm:owner"] = {
                "path": default_path,
                "provider": "local",
                "ts": 123.0,
                "items": [{"path": default_path, "provider": "local", "ts": 123.0}],
            }
            cache["dm:owner:nano_banana"] = {
                "path": nano_path,
                "provider": "gemini",
                "ts": 124.0,
                "items": [{"path": nano_path, "provider": "gemini", "ts": 124.0}],
            }

            default_reply = handler("owner", "image queue", None, "owner")
            nano_reply = handler("owner", "nano banana image queue", None, "owner")

        self.assertIn("local_image_20260606.png", default_reply)
        self.assertNotIn("nano_banana_image_20260606.png", default_reply)
        self.assertNotIn("via local", default_reply)
        self.assertIn("nano_banana_image_20260606.png", nano_reply)
        self.assertNotIn("local_image_20260606.png", nano_reply)
        self.assertNotIn("via gemini", nano_reply)

    def test_scan_requires_image_then_logs_successful_api_attempt(self):
        logged = []
        sent = []
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="scan", prompt="read it"),
            "is_owner": lambda sender: False,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "get_tool_uses_today": lambda sender, tool: 0,
            "generate_image": lambda prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
            "scan_image": lambda image_path, prompt: SimpleNamespace(ok=True, message="scan result", path=None, api_called=True),
            "log_tool_use": lambda sender, tool: logged.append((sender, tool)),
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
            "choose_generation_provider": lambda: "gemini",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        missing = handler("admin", "gpt scan image", None, "admin")
        scanned = handler("admin", "gpt scan image", "local.png", "admin")

        self.assertIn("I can read images", missing)
        self.assertIn("gpt scan", missing)
        self.assertNotIn("Current scan route", missing)
        self.assertNotIn("gemini", missing.lower())
        self.assertEqual("scan result", scanned)
        self.assertEqual([("admin", "openai_image_scan")], logged)
        self.assertTrue(any("On it, reading image" in item[1] for item in sent))

    def test_scan_recovers_recent_group_image_when_caption_row_lacks_attachment(self):
        scan_calls = []
        sent = []
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="scan", prompt="give thoughts"),
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: False,
            "is_approved_user": lambda sender: False,
            "find_recent_image_attachment": lambda recipient, sender=None: "recent.png",
            "validate_image_path": lambda image_path: (True, "", "image/png"),
            "scan_image": lambda image_path, prompt: scan_calls.append((image_path, prompt)) or SimpleNamespace(ok=True, message="scan result", path=None, api_called=True, provider="gemini"),
            "log_tool_use": lambda sender, tool: None,
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)),
            "choose_generation_provider": lambda: "gemini",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("owner", "analyze this image what do you think", None, "chat-id", is_group=True)

        self.assertEqual("scan result", reply)
        self.assertEqual([("recent.png", "give thoughts")], scan_calls)
        self.assertTrue(any("On it, reading image" in item[1] for item in sent))

    def test_scan_failure_sanitizes_provider_message(self):
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="scan", prompt="read it"),
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "scan_image": lambda image_path, prompt: SimpleNamespace(
                ok=False,
                message="Gemini image scan failed (503): Google outage",
                path=None,
                api_called=True,
                provider="gemini",
            ),
            "log_tool_use": lambda sender, tool: None,
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: None,
            "choose_generation_provider": lambda: "gemini",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 20-60 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("owner", "gpt scan image", "local.png", "owner")

        self.assertEqual(failure_copy.IMAGE_SCAN_FAILURE_REPLY, reply)
        self.assertNotIn("Gemini", reply)
        self.assertNotIn("Google", reply)
        self.assertNotIn("503", reply)

    def test_failed_non_owner_generation_still_counts_attempt(self):
        logged = []
        handler = _load_openai_image_handler({
            "parse_openai_image_intent": lambda text, has_image: SimpleNamespace(kind="generate", prompt="cat"),
            "is_owner": lambda sender: False,
            "is_admin": lambda sender: False,
            "is_approved_user": lambda sender: True,
            "get_tool_uses_today": lambda sender, tool: 0,
            "generate_image": lambda prompt: SimpleNamespace(ok=False, message="missing key", path=None, api_called=False, provider="openai"),
            "scan_image": lambda image_path, prompt: SimpleNamespace(ok=False, message="unused", path=None, api_called=False),
            "log_tool_use": lambda sender, tool: logged.append((sender, tool)),
            "send_file": lambda recipient, path, is_group=False: True,
            "send_message": lambda recipient, text, is_group=False: None,
            "choose_generation_provider": lambda: "openai",
            "choose_scan_provider": lambda: "gemini",
            "estimate_generation_time": lambda provider=None: "about 30-90 seconds",
            "estimate_scan_time": lambda provider=None: "about 10-30 seconds",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("friend", "image gen cat", None, "friend")

        self.assertIn("On it, generating image", reply)
        self.assertEqual([("friend", "openai_image_generation")], logged)

    def test_approved_friend_dm_routes_explicit_image_scan(self):
        image_buffer = {}
        sent = []
        saved = []
        routed = []

        def get_buffered_image(key, text=None):
            entry = image_buffer.get(key)
            return entry["path"] if entry else None

        def image_handler(sender, text, image_path, recipient, is_group=False):
            routed.append((sender, text, image_path, recipient, is_group))
            return "scan result"

        handler = _load_main_function("_handle_friend_dm", {
            "logger": SimpleNamespace(info=lambda *args, **kwargs: None),
            "redact_secret": lambda text: text,
            "send_message": lambda recipient, text: sent.append((recipient, text)),
            "check_admin_password": lambda text: False,
            "_strip_no_search": lambda text: (text, False),
            "_non_owner_length_rejection": lambda sender, text: None,
            "_image_buffer": image_buffer,
            "_text_buffer": {},
            "_get_buffered_text": lambda key: None,
            "_get_buffered_image": get_buffered_image,
            "_viral_banter_reply": lambda text, has_image=False: None,
            "_handle_openai_image_intent": image_handler,
            "save_turn": lambda chat, role, text: saved.append((chat, role, text)),
            "time": SimpleNamespace(time=lambda: 123.0),
            "is_ufc_fight_card_request": lambda text: False,
            "get_tool_uses_today": lambda sender, tool: 0,
            "UFC_FIGHT_CARD_TOOL": "ufc_fight_card",
            "_FRIEND_SEARCH_LIMIT": 5,
            "get_ufc_fight_card": lambda: "card",
            "get_history": lambda sender: [],
            "get_response": lambda *args, **kwargs: "llm fallback",
        })

        handler("friend", "what's in this screenshot?", "local.png")

        self.assertEqual([("friend", "scan result")], sent)
        self.assertEqual([("friend", "what's in this screenshot?", "local.png", "friend", False)], routed)
        self.assertIn(("friend", "assistant", "scan result"), saved)

    def test_unmentioned_group_image_buffers_for_later_scan(self):
        image_buffer = {}
        helper = _load_main_function(
            "_buffer_unmentioned_group_image",
            {
                "_image_buffer": image_buffer,
                "OWNER_ID": "owner",
                "is_owner_in_chat": lambda chat_id, owner_id: True,
                "is_gc_enabled": lambda chat_id: True,
                "is_owner": lambda sender: False,
                "is_approved_user": lambda sender: sender == "friend",
            },
            dependencies=("_buf_key",),
        )

        self.assertTrue(helper("friend", "chat-id", "local.png"))

        self.assertEqual("local.png", image_buffer["chat-id|friend"]["path"])

    def test_owner_group_image_buffers_even_when_gc_is_off(self):
        image_buffer = {}
        helper = _load_main_function(
            "_buffer_unmentioned_group_image",
            {
                "_image_buffer": image_buffer,
                "OWNER_ID": "owner",
                "is_owner_in_chat": lambda chat_id, owner_id: True,
                "is_gc_enabled": lambda chat_id: False,
                "is_owner": lambda sender: sender == "owner",
                "is_approved_user": lambda sender: False,
            },
            dependencies=("_buf_key",),
        )

        self.assertTrue(helper("owner", "chat-id", "local.png"))

        self.assertEqual("local.png", image_buffer["chat-id|owner"]["path"])

    def test_group_image_without_mention_is_buffered_before_early_return(self):
        calls = []
        handler = _load_main_function("handle_message", {
            "is_group_chat": lambda chat_id: True,
            "is_at_mentioned": lambda text: False,
            "_buffer_unmentioned_group_image": lambda sender, chat_id, image_path: calls.append((sender, chat_id, image_path)) or True,
            "check_rate_limit": lambda sender: (_ for _ in ()).throw(AssertionError("rate limit should not run")),
            "send_message": lambda *args, **kwargs: calls.append(("send", args, kwargs)),
            "handle_group_message": lambda *args, **kwargs: calls.append(("group", args, kwargs)),
            "handle_dm": lambda *args, **kwargs: calls.append(("dm", args, kwargs)),
            "update_heartbeat": lambda: calls.append(("heartbeat",)),
        })

        handler({"sender": "friend", "chat_identifier": "chat-id", "text": "", "image_path": "local.png"})

        self.assertEqual([("friend", "chat-id", "local.png")], calls)

    def test_image_capability_question_reports_provider_status(self):
        handler = _load_image_capability_handler({
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_provider_status": lambda: "Image routing:\n  Scan/read: available via gemini",
        })

        reply = handler("owner", "what image model are you using?")

        self.assertIn("Scan/read: available via gemini", reply)

    def test_image_scan_capability_question_knows_group_chat_flow(self):
        handler = _load_image_capability_handler({
            "is_owner": lambda sender: False,
            "is_admin": lambda sender: False,
            "is_approved_user": lambda sender: True,
            "choose_scan_provider": lambda: "gemini",
            "image_provider_status": lambda: "Image routing:\n  Scan/read: available via gemini",
        })

        reply = handler("friend", "can you scan images in group chats?")

        self.assertIn("enabled group chats", reply)
        self.assertIn("@Davos what's in this?", reply)
        self.assertNotIn("Image routing", reply)

    def test_casual_image_questions_do_not_dump_routing_status(self):
        handler = _load_image_capability_handler({
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_provider_status": lambda: "Image routing:\n  Scan/read: available via gemini",
        })

        reply = handler("owner", "can you make an image like this?")

        self.assertIsNone(reply)

    def test_image_generation_capability_question_answers_without_model(self):
        handler = _load_image_capability_handler({
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "choose_generation_provider": lambda: "local",
            "image_provider_status": lambda: "Image routing",
        })

        reply = handler("owner", "can you generate images in group chats?")

        self.assertIn("Yes", reply)
        self.assertIn("image gen", reply)
        self.assertNotIn("Image routing", reply)

    def test_log_payload_with_image_words_does_not_show_image_routing(self):
        handler = _load_image_capability_handler({
            "is_owner": lambda sender: True,
            "is_admin": lambda sender: True,
            "is_approved_user": lambda sender: True,
            "image_provider_status": lambda: "Image routing:\n  Scan/read: available via gemini",
        })

        reply = handler("owner", "Log this message entirely: This image shows image routing failed.")

        self.assertIsNone(reply)


if __name__ == "__main__":
    unittest.main()
