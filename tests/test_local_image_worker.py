import json
import tempfile
import unittest
from pathlib import Path

from scripts import local_image_worker


class _FakeResponse:
    def __init__(self, payload=None, *, status_code=200, content=b""):
        self._payload = payload or {}
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload


class _FakeComfySession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, json, timeout):
        self.posts.append((url, json, timeout))
        return _FakeResponse({"prompt_id": "prompt-123"})

    def get(self, url, timeout):
        self.gets.append((url, timeout))
        if "/history/" in url:
            return _FakeResponse({
                "prompt-123": {
                    "outputs": {
                        "save": {
                            "images": [
                                {
                                    "filename": "ComfyUI_00001_.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    }
                }
            })
        if "/view?" in url:
            return _FakeResponse(content=b"png-bytes")
        raise AssertionError(f"unexpected GET {url}")


class LocalImageWorkerTests(unittest.TestCase):
    def _config(self, tmp: str, workflow_path: Path) -> local_image_worker.LocalImageConfig:
        return local_image_worker.LocalImageConfig(
            host="127.0.0.1",
            port=7861,
            comfyui_url="http://127.0.0.1:8188",
            workflow_path=workflow_path,
            output_dir=Path(tmp) / "generated",
            timeout_seconds=5,
            poll_seconds=0.01,
            width=1024,
            height=1024,
            steps=4,
            guidance=0,
            negative_prompt="",
        )

    def test_build_workflow_replaces_flux_placeholders_with_typed_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, Path(tmp) / "workflow.json")
            template = {
                "prompt": {"inputs": {"text": "{{prompt}}"}},
                "latent": {"inputs": {"width": "{{width}}", "height": "{{height}}"}},
                "sampler": {"inputs": {"seed": "{{seed}}", "steps": "{{steps}}"}},
            }

            workflow = local_image_worker.build_workflow(
                template,
                prompt="a mountain-coded DavosBot icon",
                config=config,
                seed=123,
            )

            self.assertEqual("a mountain-coded DavosBot icon", workflow["prompt"]["inputs"]["text"])
            self.assertEqual(1024, workflow["latent"]["inputs"]["width"])
            self.assertEqual(1024, workflow["latent"]["inputs"]["height"])
            self.assertEqual(123, workflow["sampler"]["inputs"]["seed"])
            self.assertEqual(4, workflow["sampler"]["inputs"]["steps"])

    def test_missing_workflow_fails_with_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, Path(tmp) / "missing.json")

            with self.assertRaises(local_image_worker.ConfigError):
                local_image_worker.generate_image("cat", config=config)

    def test_generate_image_posts_to_comfyui_and_saves_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "workflow.json"
            workflow_path.write_text(
                json.dumps({"prompt": {"inputs": {"text": "{{prompt}}"}}}),
                encoding="utf-8",
            )
            config = self._config(tmp, workflow_path)
            session = _FakeComfySession()

            path = local_image_worker.generate_image("flux dog", config=config, session=session)

            self.assertTrue(path.exists())
            self.assertEqual(b"png-bytes", path.read_bytes())
            self.assertEqual("http://127.0.0.1:8188/prompt", session.posts[0][0])
            posted_workflow = session.posts[0][1]["prompt"]
            self.assertEqual("flux dog", posted_workflow["prompt"]["inputs"]["text"])
            self.assertTrue(any("/history/prompt-123" in call[0] for call in session.gets))
            self.assertTrue(any("/view?" in call[0] for call in session.gets))

    def test_generate_image_converts_comfyui_ui_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "workflow.json"
            workflow_path.write_text(
                json.dumps({
                    "nodes": [
                        {
                            "id": 1,
                            "type": "CheckpointLoaderSimple",
                            "inputs": [],
                            "widgets_values": ["flux1-schnell-fp8.safetensors"],
                        },
                        {
                            "id": 2,
                            "type": "CLIPTextEncode",
                            "inputs": [{"name": "clip", "link": 10}],
                            "widgets_values": ["{{prompt}}"],
                        },
                        {
                            "id": 3,
                            "type": "SaveImage",
                            "inputs": [{"name": "images", "link": 11}],
                            "widgets_values": ["{{filename_prefix}}"],
                        },
                        {
                            "id": 4,
                            "type": "MarkdownNote",
                            "inputs": [],
                            "widgets_values": ["operator note"],
                        },
                    ],
                    "links": [
                        [10, 1, 1, 2, 0, "CLIP"],
                        [11, 2, 0, 3, 0, "IMAGE"],
                    ],
                }),
                encoding="utf-8",
            )
            config = self._config(tmp, workflow_path)
            session = _FakeComfySession()

            local_image_worker.generate_image("flux bridge", config=config, session=session)

            posted_workflow = session.posts[0][1]["prompt"]
            self.assertEqual("CheckpointLoaderSimple", posted_workflow["1"]["class_type"])
            self.assertEqual("flux1-schnell-fp8.safetensors", posted_workflow["1"]["inputs"]["ckpt_name"])
            self.assertEqual("flux bridge", posted_workflow["2"]["inputs"]["text"])
            self.assertEqual(["1", 1], posted_workflow["2"]["inputs"]["clip"])
            self.assertNotIn("4", posted_workflow)


if __name__ == "__main__":
    unittest.main()
