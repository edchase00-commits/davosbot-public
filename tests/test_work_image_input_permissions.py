"""Selected synthetic bytes only: no live uploads, provider calls, or sends."""

import base64
from contextlib import ExitStack, closing, contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from davosbot import work_actions as actions, work_actions_extra as extra
from davosbot import work_bridge as bridge, work_image_input as images
from test_work_bridge import FakeTransport, NOW, comment, request

try:
    from PIL import Image
except ImportError:
    Image = None

OWNER = "+15550000001"
OTHER = "+15550000002"
CANARY = "selected-synthetic-metadata-canary"
FETCH_BLOB = images._fetch_blob


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width=32, height=16, metadata=True):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress((b"\0" + b"\xff\x00\x00" * width) * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + (chunk(b"tEXt", b"Comment\0" + CANARY.encode()) if metadata else b"")
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


def fixture(raw=None, mime="image/png"):
    raw = png() if raw is None else raw
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    args = {"question": "Which color fills this synthetic image?", "image_blob_sha": sha,
            "image_sha256": hashlib.sha256(raw).hexdigest(), "mime_type": mime,
            "acknowledge_github_retention": True}
    blob = {"sha": sha, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}
    return args, blob


class WorkImageInputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.args, self.blob = fixture()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("davosbot.config.OWNER_ID", OWNER))
        self.stack.enter_context(patch("davosbot.permissions.OWNER_ID", OWNER))
        self.fetch = self.stack.enter_context(patch.object(images, "_fetch_blob", return_value=self.blob))
        self.denial = self.stack.enter_context(patch("davosbot.image_access.image_access_denial", return_value=None))
        self.scan = self.stack.enter_context(patch("davosbot.openai_images.scan_image", return_value=SimpleNamespace(
            ok=True, message="The image is red.", api_called=True, provider="gemini")))
        from davosbot import memory
        state = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db_path = state / "synthetic-usage.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TABLE tool_usage (id INTEGER PRIMARY KEY, sender TEXT, tool TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.commit()
        self.stack.enter_context(patch.object(memory, "BOT_DB_PATH", str(self.db_path)))
        self.account = self.stack.enter_context(patch.object(memory, "log_tool_use", wraps=memory.log_tool_use))

    def execute(self, args=None, owner=OWNER):
        return actions.execute_action("images.scan", self.args if args is None else args, owner=owner)

    def test_discovery_advertises_bounded_upload_and_retention_without_runtime_access(self):
        with patch.object(extra, "_module", side_effect=AssertionError("runtime import")):
            schema = actions.action_catalogue()["images.scan"]
            self.assertTrue(schema["mutates"])
            self.assertEqual(1048576, schema["max_image_bytes"])
            self.assertEqual(4194304, schema["max_image_pixels"])
            self.assertNotEqual("unsupported", schema.get("availability"))
            self.assertTrue(schema["fields"]["acknowledge_github_retention"]["required"])

    def test_owner_authorization_precedes_fetch_and_decoder(self):
        for owner in (OTHER, "", False):
            with self.subTest(owner=owner):
                self.assertEqual("owner_required", self.execute(owner=owner)["evidence"]["code"])
        with patch("davosbot.permissions.is_owner", return_value=False):
            self.assertEqual("owner_required", self.execute()["evidence"]["code"])
        self.fetch.assert_not_called()
        self.scan.assert_not_called()

    def test_direct_adapter_still_rejects_nonowner(self):
        with self.assertRaisesRegex(ValueError, "owner_required"):
            images.scan_uploaded_image(self.args, OTHER)
        self.fetch.assert_not_called()

    def test_requires_all_fields_exact_hashes_mime_and_true_retention_ack(self):
        cases = [{k: v for k, v in self.args.items() if k != field} for field in self.args]
        for field, value in (("image_blob_sha", "../secret"), ("image_blob_sha", "a" * 39),
                             ("image_blob_sha", "A" * 40), ("image_sha256", "http://example.test"),
                             ("image_sha256", "g" * 64), ("mime_type", "image/svg+xml"),
                             ("mime_type", "image/gif"), ("question", ""), ("question", "x" * 1001),
                             ("acknowledge_github_retention", False), ("acknowledge_github_retention", 1)):
            cases.append({**self.args, field: value})
        for field in ("url", "path", "repository", "endpoint", "branch", "owner", "sender", "token", "image_data"):
            cases.append({**self.args, field: "untrusted"})
        for case in cases:
            self.assertEqual("error", self.execute(case)["status"])
        self.fetch.assert_not_called()
        self.scan.assert_not_called()

    def test_access_denial_and_missing_decoder_precede_blob_download(self):
        self.denial.return_value = "denied"
        self.assertEqual("image_access_denied", self.execute()["evidence"]["code"])
        self.denial.return_value = None
        with patch.dict(sys.modules, {"PIL": None}):
            self.assertEqual("image_decoder_unavailable", self.execute()["evidence"]["code"])
        self.fetch.assert_not_called()
        self.account.assert_not_called()

    def test_fixed_github_get_rechecks_pinned_channel_and_never_accepts_returned_url(self):
        fake = Mock(spec=bridge.GitHubTransport)
        fake.assert_channel.return_value = True
        fake._call.return_value = {**self.blob, "url": "https://untrusted.example/private"}
        # Exercise the real transport helper; the action tests patch it above.
        with patch.object(bridge, "GitHubTransport", return_value=fake):
            result = FETCH_BLOB(self.args["image_blob_sha"])
            self.assertEqual(self.blob["content"], result["content"])
            fake._call.assert_called_once_with([
                f"repos/{bridge.REPOSITORY}/git/blobs/{self.args['image_blob_sha']}", "--method", "GET"])
            fake.reset_mock()
            fake.assert_channel.return_value = False
            with self.assertRaisesRegex(ValueError, "image_channel_paused"):
                FETCH_BLOB(self.args["image_blob_sha"])
            fake._call.assert_not_called()
            fake.assert_channel.side_effect = bridge.BridgeError(CANARY)
            with self.assertRaisesRegex(ValueError, "^image_blob_unavailable$"):
                FETCH_BLOB(self.args["image_blob_sha"])

    def test_download_helper_itself_rejects_endpoint_injection(self):
        with patch.object(bridge, "GitHubTransport") as transport:
            for value in ("../contents/private", "https://example.test/a", "a" * 40 + "?ref=master", None):
                with self.assertRaisesRegex(ValueError, "^image_blob_invalid$"):
                    FETCH_BLOB(value)
            transport.assert_not_called()

    def test_blob_bounds_and_encoding_reject_before_decoding(self):
        for field, value in (("size", True), ("size", 0), ("size", images.MAX_IMAGE_BYTES + 1),
                             ("encoding", "utf-8"), ("content", "a" * (2 * images.MAX_IMAGE_BYTES + 1)),
                             ("sha", "b" * 40)):
            with self.subTest(field=field), patch.object(base64, "b64decode") as decode:
                with self.assertRaises(ValueError):
                    images._blob_bytes({**self.blob, field: value}, self.args)
                decode.assert_not_called()
        for content in ("data:image/png;base64," + self.blob["content"], self.blob["content"] + " ", "é", "%%%%"):
            with self.assertRaises(ValueError):
                images._blob_bytes({**self.blob, "content": content}, self.args)

    def test_blob_requires_git_hash_sha256_size_and_canonical_base64(self):
        for args, blob in (({**self.args, "image_sha256": "0" * 64}, self.blob),
                           (self.args, {**self.blob, "size": self.blob["size"] + 1}),
                           ({**self.args, "image_blob_sha": "a" * 40}, {**self.blob, "sha": "a" * 40})):
            with self.assertRaisesRegex(ValueError, "image_blob_mismatch"):
                images._blob_bytes(blob, args)
        wrapped = "\r\n".join(self.blob["content"][i:i + 60] for i in range(0, len(self.blob["content"]), 60))
        self.assertEqual(png(), images._blob_bytes({**self.blob, "content": wrapped}, self.args))

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_png_scan_uses_sanitized_pixels_private_temp_and_native_accounting(self):
        seen = []
        def provider(path, question):
            target = Path(path)
            seen.append(target)
            self.assertEqual(self.args["question"], question)
            self.assertNotIn(CANARY.encode(), target.read_bytes())
            with Image.open(target) as decoded:
                self.assertEqual((32, 16), decoded.size)
                self.assertEqual((255, 0, 0), decoded.getpixel((0, 0)))
                self.assertEqual({}, decoded.info)
            if os.name != "nt":
                self.assertEqual(0o600, target.stat().st_mode & 0o777)
                self.assertEqual(0o700, target.parent.stat().st_mode & 0o777)
            return SimpleNamespace(ok=True, message="Red.", api_called=True, provider="gemini")
        self.scan.side_effect = provider
        reply = self.execute()
        self.assertEqual("ok", reply["status"])
        self.assertEqual("Red.", reply["result"])
        self.assertFalse(reply["evidence"]["sent"])
        self.assertFalse(reply["evidence"]["temporary_input_retained"])
        self.account.assert_called_once_with(OWNER, "openai_image_scan")
        self.assertFalse(seen[0].parent.exists())
        self.assertNotIn(CANARY, json.dumps(reply))
        self.assertNotIn(str(seen[0]), json.dumps(reply))

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_real_native_accounting_commits_exactly_one_row_for_called_provider(self):
        self.assertEqual("ok", self.execute()["status"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual([(OWNER, "openai_image_scan")], db.execute("SELECT sender, tool FROM tool_usage").fetchall())
        self.scan.return_value = SimpleNamespace(ok=False, message="disabled", api_called=False, provider="disabled")
        self.assertEqual("error", self.execute()["status"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM tool_usage").fetchone()[0])
        self.account.assert_called_once_with(OWNER, "openai_image_scan")

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_jpeg_orientation_and_metadata_are_sanitized(self):
        output = BytesIO()
        source = Image.new("RGB", (12, 8), "blue")
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[0x010E] = CANARY
        source.save(output, format="JPEG", exif=exif)
        self.args, self.blob = fixture(output.getvalue(), "image/jpeg")
        self.fetch.return_value = self.blob
        def provider(path, question):
            with Image.open(path) as decoded:
                self.assertEqual((8, 12), decoded.size)
                self.assertEqual({}, decoded.info)
            self.assertNotIn(CANARY.encode(), Path(path).read_bytes())
            return SimpleNamespace(ok=True, message="Blue.", api_called=True, provider="openai")
        self.scan.side_effect = provider
        self.assertEqual("ok", self.execute()["status"])

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_invalid_mismatched_corrupt_and_oversized_pixels_do_not_call_provider(self):
        samples = [(b"<svg>" + CANARY.encode(), "image/png"), (png(), "image/jpeg"),
                   (png()[:-20], "image/png"), (png(width=4097, height=1), "image/png"),
                   (png(width=2049, height=2049), "image/png")]
        for raw, mime in samples:
            self.args, self.fetch.return_value = fixture(raw, mime)
            reply = self.execute()
            self.assertEqual("image_content_invalid", reply["evidence"]["code"])
            self.assertNotIn(CANARY, json.dumps(reply))
        self.scan.assert_not_called()
        self.account.assert_not_called()

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_animation_rejected(self):
        output = BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (2, 2), "blue")], duration=100, loop=0)
        self.args, self.fetch.return_value = fixture(output.getvalue())
        self.assertEqual("image_content_invalid", self.execute()["evidence"]["code"])
        self.scan.assert_not_called()

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_provider_failure_counts_attempt_without_publishing_error_or_retaining_input(self):
        seen = []
        def fail(path, _question):
            seen.append(Path(path))
            return SimpleNamespace(ok=False, message=CANARY + str(path), api_called=True, provider="gemini")
        self.scan.side_effect = fail
        reply = self.execute()
        self.assertEqual("image_scan_failed", reply["evidence"]["code"])
        self.account.assert_called_once()
        self.assertNotIn(CANARY, json.dumps(reply))
        self.assertFalse(seen[0].parent.exists())

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_provider_exception_cleanup_and_no_automatic_retry(self):
        seen = []
        def fail(path, _question):
            seen.append(Path(path))
            raise RuntimeError(CANARY)
        self.scan.side_effect = fail
        reply = self.execute()
        self.assertEqual("error", reply["status"])
        self.assertTrue(reply["evidence"]["ambiguous"])
        self.assertNotIn(CANARY, json.dumps(reply))
        self.assertEqual(1, self.scan.call_count)
        self.assertFalse(seen[0].parent.exists())

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_actual_gemini_scan_honors_budget_and_sends_only_sanitized_selected_pixels(self):
        from davosbot import openai_images as native
        native_scan = native.scan_gemini_image
        self.scan.side_effect = lambda path, question: native_scan(path, question)
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Red."}]}}]}
        with patch.object(native, "GEMINI_API_KEY", "synthetic-key"), \
                patch.object(native, "check_gemini_budget", return_value=SimpleNamespace(allowed=False)) as budget, \
                patch.object(native.requests, "post", return_value=response) as post, \
                patch.object(native, "_log_gemini_image_usage") as usage:
            denied = self.execute()
            self.assertEqual("error", denied["status"])
            self.assertFalse(denied["evidence"]["api_called"])
            post.assert_not_called()
            self.account.assert_not_called()
            budget.return_value = SimpleNamespace(allowed=True)
            reply = self.execute()
            self.assertEqual("ok", reply["status"])
            payload = post.call_args.kwargs["json"]
            self.assertNotIn("tools", payload)
            parts = payload["contents"][0]["parts"]
            self.assertEqual(self.args["question"], parts[0]["text"])
            raw = base64.b64decode(parts[1]["inline_data"]["data"])
            self.assertNotIn(CANARY.encode(), raw)
            with Image.open(BytesIO(raw)) as decoded:
                self.assertEqual((255, 0, 0), decoded.getpixel((0, 0)))
            usage.assert_called_once()
            self.account.assert_called_once()

    @unittest.skipIf(Image is None, "optional Pillow decoder not installed")
    def test_authenticated_bridge_commits_attempt_and_never_replays_scan_or_retains_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.args["question"] = "Question canary never saved in request journal"
            req = request(action="images.scan", args=self.args)
            transport = FakeTransport([comment(req)])
            @contextmanager
            def windows_lock(path):
                path.mkdir(parents=True, exist_ok=True)
                yield
            with ExitStack() as stack:
                if sys.platform == "win32":
                    stack.enter_context(patch.object(bridge, "_lock", side_effect=windows_lock))
                    stack.enter_context(patch.object(bridge, "_sync_state_directory"))
                def scan(path, question):
                    state = bridge._load(root / ".work_bridge" / "state.json")
                    self.assertEqual("started", state["records"][req["request_id"]]["phase"])
                    return SimpleNamespace(ok=True, message="Red.", api_called=True, provider="gemini")
                self.scan.side_effect = scan
                def worker():
                    return bridge.WorkBridge(root, OWNER, transport=transport,
                        validate_action=actions.validate_action, execute_action=actions.execute_action,
                        clock=lambda: NOW, revision="a" * 40)
                transport.auth_error = bridge.RequestRejected("edited_or_email_request")
                worker().poll()
                self.fetch.assert_not_called()
                transport.auth_error = None
                worker().poll()
                worker().poll()
                self.assertEqual(1, self.scan.call_count)
                self.assertEqual(1, self.account.call_count)
                self.assertEqual("completed", transport.publish_calls[0]["state"])
                journal = (root / ".work_bridge" / "state.json").read_text()
                self.assertNotIn(self.args["question"], journal)
                self.assertNotIn(self.blob["content"], journal)
                self.assertNotIn(CANARY, journal)


if __name__ == "__main__":
    unittest.main()
