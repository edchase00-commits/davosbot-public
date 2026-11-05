#!/usr/bin/env python3
"""Local image worker for DavosBot.

This worker exposes DavosBot's simple local-provider contract:

    POST /generate {"prompt": "...", "size": "1024x1024"}
    -> {"image_path": "/absolute/path/to/generated.png"}

It delegates actual rendering to a local ComfyUI server. The ComfyUI workflow
must be exported in API format and can use placeholders such as {{prompt}} and
{{seed}}. Keep workflow files under an ignored private path such as
exports/private/flux_workflow_api.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "generated" / "images"
LOG = logging.getLogger("davosbot.local_image_worker")
_UI_WIDGET_INPUTS = {
    "CheckpointLoaderSimple": ("ckpt_name",),
    "CLIPTextEncode": ("text",),
    "EmptySD3LatentImage": ("width", "height", "batch_size"),
    "FluxGuidance": ("guidance",),
    "KSampler": ("seed", "control_after_generate", "steps", "cfg", "sampler_name", "scheduler", "denoise"),
    "RandomNoise": ("noise_seed", "control_after_generate"),
    "BasicScheduler": ("scheduler", "steps", "denoise"),
    "SaveImage": ("filename_prefix",),
}
_UI_NOTE_NODE_TYPES = {"Note", "MarkdownNote"}


class ConfigError(RuntimeError):
    """Local worker is not configured enough to render images."""


@dataclass(frozen=True)
class LocalImageConfig:
    host: str
    port: int
    comfyui_url: str
    workflow_path: Path
    output_dir: Path
    timeout_seconds: float
    poll_seconds: float
    width: int
    height: int
    steps: int
    guidance: float
    negative_prompt: str


def _int_env(env: Mapping[str, str], name: str, default: str) -> int:
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _float_env(env: Mapping[str, str], name: str, default: str) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _path_from_env(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = (env.get(name) or "").strip()
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_config(env: Mapping[str, str] | None = None) -> LocalImageConfig:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    source = env or os.environ

    workflow_path = _path_from_env(
        source,
        "COMFYUI_WORKFLOW_PATH",
        PROJECT_ROOT / "exports" / "private" / "flux_workflow_api.json",
    )
    output_dir = _path_from_env(source, "LOCAL_IMAGE_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
    return LocalImageConfig(
        host=(source.get("LOCAL_IMAGE_WORKER_HOST") or "127.0.0.1").strip(),
        port=_int_env(source, "LOCAL_IMAGE_WORKER_PORT", "7861"),
        comfyui_url=(source.get("COMFYUI_URL") or "http://127.0.0.1:8188").strip().rstrip("/"),
        workflow_path=workflow_path,
        output_dir=output_dir,
        timeout_seconds=_float_env(source, "LOCAL_IMAGE_TIMEOUT", "180"),
        poll_seconds=max(0.25, _float_env(source, "LOCAL_IMAGE_POLL_SECONDS", "2")),
        width=_int_env(source, "FLUX_IMAGE_WIDTH", "1024"),
        height=_int_env(source, "FLUX_IMAGE_HEIGHT", "1024"),
        steps=_int_env(source, "FLUX_IMAGE_STEPS", "4"),
        guidance=_float_env(source, "FLUX_IMAGE_GUIDANCE", "0"),
        negative_prompt=source.get("FLUX_NEGATIVE_PROMPT", "") or "",
    )


def _safe_prefix(prefix: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "_", prefix).strip("._-")
    return clean[:80] or "davosbot_flux"


def _replace_placeholders(value: Any, replacements: Mapping[str, Any], env: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements, env) for item in value]
    if isinstance(value, str):
        text = value
        exact = re.fullmatch(r"\{\{([a-zA-Z0-9_]+)\}\}", text)
        if exact and exact.group(1) in replacements:
            return replacements[exact.group(1)]
        for key, replacement in replacements.items():
            text = text.replace(f"{{{{{key}}}}}", str(replacement))

        def env_replace(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2) or ""
            return env.get(name, default)

        return re.sub(r"\{\{ENV:([A-Z0-9_]+)(?::([^}]*))?\}\}", env_replace, text)
    return value


def load_workflow_template(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"ComfyUI workflow is missing at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"ComfyUI workflow JSON is invalid: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError("ComfyUI workflow must be a JSON object exported in API format")
    return data


def build_workflow(
    template: dict[str, Any],
    *,
    prompt: str,
    config: LocalImageConfig,
    seed: int | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    seed = seed if seed is not None else random.randint(1, 2**31 - 1)
    replacements = {
        "prompt": prompt,
        "negative_prompt": config.negative_prompt,
        "width": config.width,
        "height": config.height,
        "steps": config.steps,
        "guidance": config.guidance,
        "seed": seed,
        "filename_prefix": _safe_prefix(f"davosbot_flux_{time.strftime('%Y%m%d_%H%M%S')}"),
    }
    return _replace_placeholders(template, replacements, env or os.environ)


def _ui_workflow_to_api(workflow: dict[str, Any]) -> dict[str, Any]:
    nodes = workflow.get("nodes")
    links = workflow.get("links", [])
    if not isinstance(nodes, list):
        return workflow

    skipped_ids = {
        str(node.get("id"))
        for node in nodes
        if node.get("type") in _UI_NOTE_NODE_TYPES
    }
    link_map: dict[int, tuple[str, int]] = {}
    for link in links:
        if not isinstance(link, list) or len(link) < 5:
            continue
        link_id, origin_id, origin_slot = link[0], str(link[1]), link[2]
        if origin_id in skipped_ids:
            continue
        try:
            link_map[int(link_id)] = (origin_id, int(origin_slot))
        except (TypeError, ValueError):
            continue

    api: dict[str, Any] = {}
    for node in nodes:
        node_id = str(node.get("id"))
        class_type = node.get("type")
        if not node_id or not class_type or class_type in _UI_NOTE_NODE_TYPES:
            continue
        inputs: dict[str, Any] = {}

        widget_names = _UI_WIDGET_INPUTS.get(str(class_type), ())
        for name, value in zip(widget_names, node.get("widgets_values", []) or []):
            inputs[name] = value

        for input_def in node.get("inputs", []) or []:
            if not isinstance(input_def, dict):
                continue
            link_id = input_def.get("link")
            name = input_def.get("name")
            if link_id is None or not name:
                continue
            try:
                origin = link_map.get(int(link_id))
            except (TypeError, ValueError):
                origin = None
            if origin:
                inputs[str(name)] = [origin[0], origin[1]]

        api[node_id] = {"class_type": class_type, "inputs": inputs}

    if not api:
        raise ConfigError("ComfyUI UI workflow could not be converted to API format")
    return api


def _submit_prompt(config: LocalImageConfig, workflow: dict[str, Any], session=requests) -> str:
    response = session.post(
        f"{config.comfyui_url}/prompt",
        json={"prompt": workflow, "client_id": str(uuid.uuid4())},
        timeout=15,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI prompt submit failed with HTTP {response.status_code}")
    payload = response.json()
    prompt_id = payload.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise RuntimeError("ComfyUI did not return a prompt_id")
    return prompt_id


def _poll_history(config: LocalImageConfig, prompt_id: str, session=requests) -> list[dict[str, str]]:
    deadline = time.monotonic() + config.timeout_seconds
    while time.monotonic() < deadline:
        response = session.get(f"{config.comfyui_url}/history/{prompt_id}", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(f"ComfyUI history failed with HTTP {response.status_code}")
        history = response.json()
        item = history.get(prompt_id) if isinstance(history, dict) else None
        outputs = item.get("outputs", {}) if isinstance(item, dict) else {}
        images: list[dict[str, str]] = []
        for output in outputs.values():
            for image in output.get("images", []) or []:
                if isinstance(image, dict) and image.get("filename"):
                    images.append(
                        {
                            "filename": str(image.get("filename", "")),
                            "subfolder": str(image.get("subfolder", "")),
                            "type": str(image.get("type", "output")),
                        }
                    )
        if images:
            return images
        time.sleep(config.poll_seconds)
    raise TimeoutError("ComfyUI image generation timed out")


def _download_image(config: LocalImageConfig, image: Mapping[str, str], index: int, session=requests) -> Path:
    query = urlencode(
        {
            "filename": image.get("filename", ""),
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        }
    )
    response = session.get(f"{config.comfyui_url}/view?{query}", timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI image download failed with HTTP {response.status_code}")
    suffix = Path(image.get("filename", "")).suffix or ".png"
    config.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.output_dir / f"local_flux_{time.strftime('%Y%m%d_%H%M%S')}_{index}{suffix}"
    out_path.write_bytes(response.content)
    return out_path


def generate_image(prompt: str, config: LocalImageConfig | None = None, session=requests) -> Path:
    config = config or load_config()
    prompt = " ".join((prompt or "").split()).strip()
    if not prompt:
        raise ConfigError("prompt is required")
    template = load_workflow_template(config.workflow_path)
    workflow = build_workflow(template, prompt=prompt, config=config)
    workflow = _ui_workflow_to_api(workflow)
    prompt_id = _submit_prompt(config, workflow, session=session)
    images = _poll_history(config, prompt_id, session=session)
    return _download_image(config, images[0], 0, session=session)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class LocalImageHandler(BaseHTTPRequestHandler):
    server_version = "DavosLocalImageWorker/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        config = load_config()
        _json_response(
            self,
            200,
            {
                "ok": True,
                "configured": config.workflow_path.exists(),
                "comfyui_url": config.comfyui_url,
                "workflow_exists": config.workflow_path.exists(),
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/generate":
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or "0"), 100_000)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            prompt = str(payload.get("prompt", "")).strip()
            path = generate_image(prompt)
            _json_response(self, 200, {"ok": True, "image_path": str(path), "provider": "local_flux"})
        except ConfigError as exc:
            _json_response(self, 503, {"ok": False, "error": str(exc)})
        except TimeoutError:
            _json_response(self, 504, {"ok": False, "error": "local image generation timed out"})
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            LOG.exception("local image generation failed")
            _json_response(self, 500, {"ok": False, "error": f"local image generation failed: {type(exc).__name__}"})

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def serve(config: LocalImageConfig) -> None:
    server = ThreadingHTTPServer((config.host, config.port), LocalImageHandler)
    LOG.info("local image worker listening on http://%s:%s", config.host, config.port)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", help="Generate one image for this prompt, then exit.")
    parser.add_argument("--health", action="store_true", help="Print health/config status as JSON and exit.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOCAL_IMAGE_LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    config = load_config()

    if args.health:
        print(json.dumps({"configured": config.workflow_path.exists(), "workflow_path": str(config.workflow_path), "comfyui_url": config.comfyui_url}, sort_keys=True))
        return 0
    if args.once:
        path = generate_image(args.once, config)
        print(json.dumps({"image_path": str(path), "provider": "local_flux"}, sort_keys=True))
        return 0

    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
