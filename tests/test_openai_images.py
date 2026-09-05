import base64
import tempfile
import unittest
from pathlib import Path

from davosbot import openai_images
class _FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class OpenAIImageTests(unittest.TestCase):
    def tearDown(self):
        openai_images.OPENAI_API_KEY = ""
        openai_images.OPENAI_IMAGE_MODEL = ""
        openai_images.OPENAI_VISION_MODEL = ""
        openai_images.GEMINI_API_KEY = ""
        openai_images.LOCAL_IMAGE_ENDPOINT = ""
        openai_images.IMAGE_PROVIDER = "auto"
        openai_images.IMAGE_SCAN_PROVIDER = "auto"

    def test_parser_detects_explicit_generation_without_grabbing_generate_file(self):
        intent = openai_images.parse_openai_image_intent("image gen a sharp DavosBot logo")

        self.assertEqual("generate", intent.kind)
        self.assertIn("DavosBot logo", intent.prompt)
        self.assertIsNone(openai_images.parse_openai_image_intent("generate file called report.csv"))

    def test_parser_accepts_direct_addressed_gemini_generation(self):
        intent = openai_images.parse_openai_image_intent("Davos image gen a fish using Gemini")

        self.assertEqual("generate", intent.kind)
        self.assertEqual("a fish", intent.prompt)

    def test_parser_detects_natural_generation_phrases(self):
        examples = [
            "make me a logo for DavosBot",
            "can you draw me an icon for the group chat",
            "please create a banner: pacers watch party",
            "Gemini image gen a mountain logo",
        ]

        for example in examples:
            with self.subTest(example=example):
                intent = openai_images.parse_openai_image_intent(example)
                self.assertEqual("generate", intent.kind)
                self.assertTrue(intent.prompt)

    def test_parser_detects_scan_only_when_image_or_gpt_is_explicit(self):
        self.assertIsNone(openai_images.parse_openai_image_intent("scan main.py", has_image=False))

        intent = openai_images.parse_openai_image_intent("gpt scan image read the text", has_image=False)
        self.assertEqual("scan", intent.kind)
        self.assertEqual("read the text", intent.prompt)

        attached = openai_images.parse_openai_image_intent("scan this for errors", has_image=True)
        self.assertEqual("scan", attached.kind)

    def test_parser_detects_natural_attached_image_scan_phrases(self):
        examples = [
            "what's in this screenshot?",
            "who is this?",
            "identify this guy",
            "can you read this",
            "look at this and tell me what matters",
            "what does this say",
        ]

        for example in examples:
            with self.subTest(example=example):
                intent = openai_images.parse_openai_image_intent(example, has_image=True)
                self.assertEqual("scan", intent.kind)

    def test_parser_tolerates_common_analyze_typos_for_attached_image_scan(self):
        examples = [
            "anaylze this",
            "anlyze this for errors",
            "analzye this screenshot",
        ]

        for example in examples:
            with self.subTest(example=example):
                intent = openai_images.parse_openai_image_intent(example, has_image=True)
                self.assertEqual("scan", intent.kind)

    def test_parser_routes_ambiguous_attached_image_asks_to_scan(self):
        examples = [
            "what do you think?",
            "why is it doing that",
            "does this look right?",
            "thoughts?",
            "explain what is happening here",
        ]

        for example in examples:
            with self.subTest(example=example):
                intent = openai_images.parse_openai_image_intent(example, has_image=True)
                self.assertEqual("scan", intent.kind)
                self.assertTrue(intent.prompt)

        self.assertIsNone(openai_images.parse_openai_image_intent("what do you think?", has_image=False))

    def test_parser_detects_reference_generation_with_attached_image(self):
        examples = [
            "make an image based on this",
            "can you create a logo like this but cleaner",
            "turn this into a sticker for Cole",
        ]

        for example in examples:
            with self.subTest(example=example):
                intent = openai_images.parse_openai_image_intent(example, has_image=True)
                self.assertEqual("generate", intent.kind)
                self.assertTrue(intent.prompt)

    def test_attached_scan_keeps_the_users_question(self):
        intent = openai_images.parse_openai_image_intent("what's in this screenshot?", has_image=True)

        self.assertEqual("scan", intent.kind)
        self.assertEqual("what's in this screenshot?", intent.prompt)
        for question in ("what does this say", "read this for errors", "who is this?"):
            with self.subTest(question=question):
                parsed = openai_images.parse_openai_image_intent(question, has_image=True)
                self.assertEqual(question, parsed.prompt)

    def test_attached_image_opinion_requests_are_brief(self):
        intent = openai_images.parse_openai_image_intent("what do u think of this guy", has_image=True)

        self.assertEqual("scan", intent.kind)
        self.assertIn("1-2 short sentences", intent.prompt)
        self.assertIn("skip the full image audit", intent.prompt)
        self.assertIn("User ask: what do u think of this guy", intent.prompt)

    def test_attached_image_roast_requests_are_short_and_styled(self):
        intent = openai_images.parse_openai_image_intent("roast", has_image=True)

        self.assertEqual("scan", intent.kind)
        self.assertIn("Roast this image in 1-3 short lines", intent.prompt)
        self.assertIn("specific to visible details", intent.prompt)
        self.assertIn("No disclaimers", intent.prompt)

    def test_explicit_image_scan_roast_can_use_atl_style_without_inline_image(self):
        intent = openai_images.parse_openai_image_intent("image scan and roast in atl", has_image=False)

        self.assertEqual("scan", intent.kind)
        self.assertIn("Roast this image in 1-3 short lines", intent.prompt)
        self.assertIn("ATL/Atlanta persona style", intent.prompt)
        self.assertIn("User ask: and roast in atl", intent.prompt)

    def test_food_roast_phrase_does_not_force_image_roast_prompt(self):
        intent = openai_images.parse_openai_image_intent("roast chicken", has_image=True)

        self.assertEqual("scan", intent.kind)
        self.assertEqual("roast chicken", intent.prompt)

    def test_scan_posts_responses_api_with_data_url(self):
        calls = []

        def fake_post(url, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            return _FakeResponse({"output_text": "visible text: hello"})

        old_post = openai_images.requests.post
        old_key = openai_images.OPENAI_API_KEY
        old_model = openai_images.OPENAI_VISION_MODEL
        try:
            openai_images.requests.post = fake_post
            openai_images.OPENAI_API_KEY = "test-key"
            openai_images.OPENAI_VISION_MODEL = "legacy-vision-test"
            with tempfile.TemporaryDirectory() as tmp:
                image_path = Path(tmp) / "sample.jpg"
                image_path.write_bytes(b"fake-jpeg")

                result = openai_images.scan_openai_image(str(image_path), "read this")

            self.assertTrue(result.ok)
            self.assertTrue(result.api_called)
            self.assertEqual("visible text: hello", result.message)
            self.assertEqual("https://api.openai.com/v1/responses", calls[0][0])
            image_part = calls[0][2]["input"][0]["content"][1]
            self.assertTrue(image_part["image_url"].startswith("data:image/jpeg;base64,"))
        finally:
            openai_images.requests.post = old_post
            openai_images.OPENAI_API_KEY = old_key
            openai_images.OPENAI_VISION_MODEL = old_model

    def test_validate_image_path_reports_missing_file_without_path_leak(self):
        ok, reason, mime = openai_images.validate_image_path("/tmp/definitely_missing_secret_name.png")

        self.assertFalse(ok)
        self.assertIn("not found", reason)
        self.assertEqual("", mime)
        self.assertNotIn("secret_name", reason)

    def test_validate_image_path_infers_heic_mime_from_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "sample.HEIC"
            image_path.write_bytes(b"heic-ish")

            ok, reason, mime = openai_images.validate_image_path(str(image_path))

        self.assertTrue(ok, reason)
        self.assertEqual("image/heic", mime)

    def test_generation_saves_b64_png_to_ignored_generated_folder(self):
        def fake_post(url, headers, json, timeout):
            png = base64.b64encode(b"png-bytes").decode("ascii")
            return _FakeResponse({"data": [{"b64_json": png}]})

        old_post = openai_images.requests.post
        old_key = openai_images.OPENAI_API_KEY
        old_model = openai_images.OPENAI_IMAGE_MODEL
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images.OPENAI_API_KEY = "test-key"
            openai_images.OPENAI_IMAGE_MODEL = "legacy-image-test"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_openai_image("a tiny blue icon")

                self.assertTrue(result.ok)
                self.assertTrue(result.api_called)
                self.assertTrue(Path(result.path).exists())
                self.assertEqual(b"png-bytes", Path(result.path).read_bytes())
        finally:
            openai_images.requests.post = old_post
            openai_images.OPENAI_API_KEY = old_key
            openai_images.OPENAI_IMAGE_MODEL = old_model
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir

    def test_generation_provider_follows_explicit_image_provider(self):
        for provider in ("openai", "gemini", "local", "disabled"):
            with self.subTest(provider=provider):
                openai_images.IMAGE_PROVIDER = provider
                self.assertEqual(provider, openai_images.choose_generation_provider())

        openai_images.IMAGE_PROVIDER = "bogus"
        self.assertEqual("disabled", openai_images.choose_generation_provider())

    def test_auto_generation_provider_prefers_local_then_gemini_and_never_openai(self):
        openai_images.IMAGE_PROVIDER = "auto"
        openai_images.LOCAL_IMAGE_ENDPOINT = "http://127.0.0.1:8188/generate"
        openai_images.GEMINI_API_KEY = "gemini-key"
        openai_images.OPENAI_API_KEY = "openai-key"
        self.assertEqual("local", openai_images.choose_generation_provider())

        openai_images.LOCAL_IMAGE_ENDPOINT = ""
        self.assertEqual("gemini", openai_images.choose_generation_provider())

        openai_images.GEMINI_API_KEY = ""
        self.assertEqual("disabled", openai_images.choose_generation_provider())

    def test_disabled_generation_provider_fails_closed(self):
        openai_images.IMAGE_PROVIDER = "disabled"

        result = openai_images.generate_image("cat")

        self.assertFalse(result.ok)
        self.assertFalse(result.api_called)
        self.assertEqual("disabled", result.provider)
        self.assertIn("image generation is disabled", result.message)

    def test_openai_provider_missing_key_has_clear_error(self):
        openai_images.IMAGE_PROVIDER = "openai"
        openai_images.OPENAI_API_KEY = ""

        result = openai_images.generate_image("cat")

        self.assertFalse(result.ok)
        self.assertFalse(result.api_called)
        self.assertEqual("openai", result.provider)
        self.assertIn("OpenAI image feature is not configured", result.message)

    def test_gemini_generation_saves_inline_image(self):
        calls = []

        def fake_post(url, params, json, timeout):
            calls.append(json)
            png = base64.b64encode(b"gemini-png").decode("ascii")
            return _FakeResponse({
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Here you go"},
                                {"inlineData": {"mimeType": "image/png", "data": png}},
                            ]
                        }
                    }
                ]
            })

        old_post = openai_images.requests.post
        old_sdk = openai_images._generate_gemini_image_via_sdk
        old_key = openai_images.GEMINI_API_KEY
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images._generate_gemini_image_via_sdk = lambda *args, **kwargs: None
            openai_images.GEMINI_API_KEY = "gemini-key"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_gemini_image("a tiny logo")

                self.assertTrue(result.ok)
                self.assertEqual("gemini", result.provider)
                self.assertEqual(b"gemini-png", Path(result.path).read_bytes())
                self.assertIn("Generate an image", calls[0]["contents"][0]["parts"][0]["text"])
                self.assertNotIn("generationConfig", calls[0])
        finally:
            openai_images.requests.post = old_post
            openai_images._generate_gemini_image_via_sdk = old_sdk
            openai_images.GEMINI_API_KEY = old_key
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir

    def test_nano_banana_generation_uses_2k_response_format_and_separate_prefix(self):
        calls = []

        def fake_post(url, params, json, timeout):
            calls.append((url, json))
            png = base64.b64encode(b"nano-png").decode("ascii")
            return _FakeResponse({
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": png}},
                            ]
                        }
                    }
                ]
            })

        old_post = openai_images.requests.post
        old_sdk = openai_images._generate_gemini_image_via_sdk
        old_key = openai_images.GEMINI_API_KEY
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images._generate_gemini_image_via_sdk = lambda *args, **kwargs: None
            openai_images.GEMINI_API_KEY = "gemini-key"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_nano_banana_image("a sharp logo via nano banana")

                self.assertTrue(result.ok)
                self.assertEqual("gemini", result.provider)
                self.assertEqual(b"nano-png", Path(result.path).read_bytes())
                self.assertTrue(Path(result.path).name.startswith("nano_banana_image_"))
                self.assertIn("/v1/models/gemini-3.1-flash-image:generateContent", calls[0][0])
                response_format = calls[0][1]["generationConfig"]["responseFormat"]["image"]
                self.assertEqual("2K", response_format["imageSize"])
                self.assertEqual("1:1", response_format["aspectRatio"])
                self.assertNotIn("responseModalities", calls[0][1]["generationConfig"])
                prompt_text = calls[0][1]["contents"][0]["parts"][0]["text"]
                self.assertNotIn("nano banana", prompt_text.lower())
        finally:
            openai_images.requests.post = old_post
            openai_images._generate_gemini_image_via_sdk = old_sdk
            openai_images.GEMINI_API_KEY = old_key
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir

    def test_gemini_generation_retries_default_when_rest_config_is_rejected(self):
        calls = []

        def fake_post(url, params, json, timeout):
            calls.append(json)
            if "generationConfig" in json:
                return _FakeResponse(
                    {
                        "error": {
                            "message": "Invalid JSON payload received. Unknown name \"responseFormat\" at 'generation_config': Cannot find field."
                        }
                    },
                    status_code=400,
                )
            png = base64.b64encode(b"fallback-png").decode("ascii")
            return _FakeResponse({
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": png}},
                            ]
                        }
                    }
                ]
            })

        old_post = openai_images.requests.post
        old_sdk = openai_images._generate_gemini_image_via_sdk
        old_key = openai_images.GEMINI_API_KEY
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images._generate_gemini_image_via_sdk = lambda *args, **kwargs: None
            openai_images.GEMINI_API_KEY = "gemini-key"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_nano_banana_image("a sharp logo")

                self.assertTrue(result.ok)
                self.assertEqual(b"fallback-png", Path(result.path).read_bytes())
                self.assertEqual(2, len(calls))
                self.assertIn("generationConfig", calls[0])
                self.assertNotIn("generationConfig", calls[1])
                self.assertIn("default size", result.message)
        finally:
            openai_images.requests.post = old_post
            openai_images._generate_gemini_image_via_sdk = old_sdk
            openai_images.GEMINI_API_KEY = old_key
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir

    def test_gemini_generation_expands_short_davosbot_prompt(self):
        prompt = openai_images._generation_prompt_for_provider("davos bot", "gemini")

        self.assertIn("Generate an image", prompt)
        self.assertIn("private iMessage AI assistant", prompt)
        self.assertIn("DavosBot", prompt)

    def test_scan_auto_prefers_gemini_when_configured(self):
        openai_images.IMAGE_SCAN_PROVIDER = "auto"
        openai_images.GEMINI_API_KEY = "gemini-key"
        openai_images.OPENAI_API_KEY = "openai-key"

        self.assertEqual("gemini", openai_images.choose_scan_provider())

    def test_image_provider_status_explains_legacy_gpt_scan_wording(self):
        openai_images.IMAGE_PROVIDER = "auto"
        openai_images.IMAGE_SCAN_PROVIDER = "auto"
        openai_images.LOCAL_IMAGE_ENDPOINT = "http://127.0.0.1:7861/generate"
        openai_images.GEMINI_API_KEY = "gemini-key"
        openai_images.OPENAI_API_KEY = ""

        reply = openai_images.image_provider_status()

        self.assertIn("Generation: available via local", reply)
        self.assertIn("Scan/read: available via gemini", reply)
        self.assertIn("Nano Banana: gemini-3.1-flash-image; output 2K 1:1; explicit only; 2K uses google-genai SDK when installed.", reply)
        self.assertIn("OpenAI legacy: not configured; not used by auto routes.", reply)
        self.assertIn("`gpt scan image` is legacy wording", reply)

    def test_missing_key_does_not_call_api(self):
        openai_images.OPENAI_API_KEY = ""

        result = openai_images.generate_openai_image("cat")

        self.assertFalse(result.ok)
        self.assertFalse(result.api_called)
        self.assertIn("not configured", result.message)

    def test_local_generation_expands_davosbot_prompt(self):
        calls = []

        def fake_post(url, json, timeout):
            calls.append(json)
            png = base64.b64encode(b"local-png").decode("ascii")
            return _FakeResponse({"image_base64": png, "mime_type": "image/png"})

        old_post = openai_images.requests.post
        old_endpoint = openai_images.LOCAL_IMAGE_ENDPOINT
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images.LOCAL_IMAGE_ENDPOINT = "http://127.0.0.1:7861/generate"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_local_image("davos bot")

                self.assertTrue(result.ok)
                self.assertEqual("local", result.provider)
                self.assertIn("private iMessage AI assistant", calls[0]["prompt"])
                self.assertEqual("local", calls[0]["provider"])
        finally:
            openai_images.requests.post = old_post
            openai_images.LOCAL_IMAGE_ENDPOINT = old_endpoint
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir

    def test_auto_generation_falls_back_when_local_provider_fails(self):
        calls = []

        def fake_post(url, json=None, params=None, timeout=None):
            calls.append(url)
            if "127.0.0.1" in url:
                return _FakeResponse({"error": "worker down"}, status_code=503, text="worker down")
            png = base64.b64encode(b"gemini-png").decode("ascii")
            return _FakeResponse({
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inlineData": {"mimeType": "image/png", "data": png}},
                            ]
                        }
                    }
                ]
            })

        old_post = openai_images.requests.post
        old_sdk = openai_images._generate_gemini_image_via_sdk
        old_endpoint = openai_images.LOCAL_IMAGE_ENDPOINT
        old_key = openai_images.GEMINI_API_KEY
        old_dir = openai_images.OPENAI_IMAGE_OUTPUT_DIR
        try:
            openai_images.requests.post = fake_post
            openai_images._generate_gemini_image_via_sdk = lambda *args, **kwargs: None
            openai_images.IMAGE_PROVIDER = "auto"
            openai_images.LOCAL_IMAGE_ENDPOINT = "http://127.0.0.1:7861/generate"
            openai_images.GEMINI_API_KEY = "gemini-key"
            with tempfile.TemporaryDirectory() as tmp:
                openai_images.OPENAI_IMAGE_OUTPUT_DIR = tmp
                result = openai_images.generate_image("cat")

                self.assertTrue(result.ok)
                self.assertEqual("gemini", result.provider)
                self.assertTrue(any("127.0.0.1" in url for url in calls))
                self.assertTrue(any("generativelanguage" in url for url in calls))
        finally:
            openai_images.requests.post = old_post
            openai_images._generate_gemini_image_via_sdk = old_sdk
            openai_images.LOCAL_IMAGE_ENDPOINT = old_endpoint
            openai_images.GEMINI_API_KEY = old_key
            openai_images.OPENAI_IMAGE_OUTPUT_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
