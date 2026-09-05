import base64
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import (
    GEMINI_API_KEY,
    GEMINI_IMAGE_API_VERSION,
    GEMINI_IMAGE_MODEL,
    IMAGE_PROVIDER,
    IMAGE_SCAN_PROVIDER,
    LOCAL_IMAGE_ENDPOINT,
    LOCAL_IMAGE_MODEL,
    LOCAL_IMAGE_TIMEOUT,
    NANO_BANANA_IMAGE_ASPECT_RATIO,
    NANO_BANANA_IMAGE_MODEL,
    NANO_BANANA_IMAGE_SIZE,
    OPENAI_API_KEY,
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_OUTPUT_DIR,
    OPENAI_IMAGE_QUALITY,
    OPENAI_IMAGE_SIZE,
    OPENAI_VISION_MODEL,
    PROJECT_ROOT,
)
from .billing import check_gemini_budget, log_gemini_usage
from .permissions import redact_secret


logger = logging.getLogger(__name__)

OPENAI_IMAGE_SCAN_TOOL = "openai_image_scan"
OPENAI_IMAGE_GENERATION_TOOL = "openai_image_generation"

_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_OPENAI_IMAGE_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
_GEMINI_GENERATE_URL_TEMPLATE = "https://generativelanguage.googleapis.com/{api_version}/models/{model}:generateContent"
_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_MAX_PROMPT_CHARS = 1600
_KNOWN_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
_VALID_IMAGE_PROVIDERS = {"auto", "disabled", "local", "gemini", "openai"}
_VALID_SCAN_PROVIDERS = {"auto", "disabled", "gemini", "openai"}
_DEFAULT_SCAN_PROMPT = "Analyze this image. Call out visible text, likely meaning, and anything actionable."
_DAVOSBOT_IMAGE_BRIEF = (
    "DavosBot is a private iMessage AI assistant. Visualize it as a polished modern bot brand: "
    "smart, friendly, a little Swiss/Davos mountain-coded, clean geometric mark, premium tech feel, "
    "no tiny unreadable text."
)
_DIRECT_ADDRESS_RE = re.compile(r"^\s*@?\s*davos(?:bot)?\b[\s,;:.-]*", re.IGNORECASE)


@dataclass(frozen=True)
class OpenAIImageIntent:
    kind: str
    prompt: str


@dataclass(frozen=True)
class OpenAIImageResult:
    ok: bool
    message: str
    path: str | None = None
    api_called: bool = False
    provider: str = ""


_IMAGE_NOUN = (
    r"(?:image|picture|photo|graphic|illustration|art|drawing|screenshot|"
    r"logo|icon|avatar|banner|poster|sticker|meme|wallpaper|mockup)"
)
_POLITE_PREFIX = r"(?:(?:can|could|would)\s+you\s+|please\s+)?"
_SCAN_VERB = r"(?:scan|analy[sz]e|anayl[sz]e|anal[sz]ye|anly[sz]e|analzye|anyl[sz]e|anali[sz]e|describe|read|inspect)"
_GENERATION_RE = re.compile(
    rf"^\s*(?:"
    rf"image\s+(?:gen|generate)|"
    rf"(?:gemini|google\s+gemini)\s+image(?:\s+(?:gen|generate))?|"
    rf"(?:gemini|google\s+gemini)\s+(?:generate|create|make|draw|render)\s+(?:me\s+)?(?:an?\s+)?{_IMAGE_NOUN}|"
    rf"(?:gpt|openai)\s+image(?:\s+(?:gen|generate))?|"
    rf"(?:gpt|openai)\s+(?:generate|create|make|draw|render)\s+(?:me\s+)?(?:an?\s+)?{_IMAGE_NOUN}|"
    rf"{_POLITE_PREFIX}(?:generate|create|make|draw|render)\s+(?:me\s+)?(?:an?\s+)?{_IMAGE_NOUN}"
    rf")\s*(?:of|for|about|:)?\s*(?P<prompt>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_SCAN_IMAGE_RE = re.compile(
    rf"^\s*(?:(?:gpt|openai)\s+)?(?:"
    rf"{_SCAN_VERB}\s+(?:this\s+)?{_IMAGE_NOUN}\b"
    rf"|{_IMAGE_NOUN}\s+{_SCAN_VERB}\b"
    rf")\s*(?P<prompt>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_SCAN_GPT_RE = re.compile(
    rf"^\s*(?:gpt|openai)\s+(?:vision|{_SCAN_VERB})\b\s*(?P<prompt>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_SCAN_WITH_ATTACHED_IMAGE_RE = re.compile(
    r"^\s*(?:(?:gpt|openai)\s+)?(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:"
    rf"(?:{_SCAN_VERB}|view|look\s+at|check|see)(?:\s+this)?\b"
    r"|what(?:'s|\s+is)\s+(?:in|on)\s+this\b"
    r"|what(?:'s|\s+is)\s+this\b"
    r"|what\s+does\s+this\s+say\b"
    r"|who\s+(?:is|are)\s+(?:this|that|in\s+this)\b"
    r"|identify\s+(?:this|that|the\s+(?:person|guy|player|character|celebrity|thing))\b"
    r"|tell\s+me\s+(?:what\s+)?(?:is|what's)\s+(?:in|on)\s+this\b"
    r"|what\s+am\s+i\s+looking\s+at\b"
    r")\s*(?P<prompt>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ATTACHED_IMAGE_SCAN_CONTEXT_RE = re.compile(
    r"\b(?:this|that|it|here|image|photo|picture|screenshot|screen|attachment|meme|chart|text)\b"
    r"|^\s*(?:what|why|how|who|where|which|does|do|is|are|can|could|would|should)\b"
    rf"|^\s*(?:thoughts?|opinion|explain|caption|roast|cook|flame|drag|clown|{_SCAN_VERB}|look|check|tell)\b",
    re.IGNORECASE | re.DOTALL,
)
_VAGUE_ATTACHED_IMAGE_SCAN_RE = re.compile(
    r"^\s*(?:this|that|it|here|thoughts?|opinion|what|why|how|who|where|which|look|check|see|\?+)\s*[.?!:;-]*\s*$",
    re.IGNORECASE,
)
_REFERENCE_GENERATION_WITH_ATTACHED_IMAGE_RE = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:(?:nano\s*banana|gemini)\s+)?"
    r"(?:(?:make|create|generate|draw|render|turn|remix|edit|recreate|redo)\b"
    r".{0,120}\b(?:image|picture|photo|graphic|illustration|art|drawing|logo|icon|meme|sticker|this|that|it)"
    r"|(?:use|base|based)\b.{0,120}\b(?:this|that|image|picture|photo|screenshot)\b"
    r"|(?:make|create|generate|draw|render)\b.{0,120}\b(?:like|from|based\s+on)\b.{0,120}\b(?:this|that|image|picture|photo|screenshot))"
    r"(?P<prompt>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_NANO_BANANA_GENERATION_RE = re.compile(
    rf"^\s*nano\s*banana\b\s*"
    rf"(?:(?:image|img)\s*(?:gen|generate)?|"
    rf"(?:generate|create|make|draw|render)\s+(?:me\s+)?(?:an?\s+)?(?:{_IMAGE_NOUN})?|"
    rf"(?:{_IMAGE_NOUN})\s*(?:of|for|about|:))"
    rf"\s*(?:of|for|about|:)?\s*(?P<prompt>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_FOOD_ROAST_RE = re.compile(
    r"\b(?:roast(?:ing|ed)?|cook(?:ing|ed)?)\s+(?:the\s+|these\s+|some\s+)?"
    r"(?:chicken|turkey|beef|pork|vegetables?|potatoes?|wings?|food|dinner|lunch|breakfast|meal|steak|rice|pasta)\b",
    re.IGNORECASE,
)
_IMAGE_ROAST_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|no|without|stop|avoid)\s+(?:a\s+)?(?:roast(?:ing)?|cook(?:ing)?|flam(?:e|ing)|drag(?:ging)?|clown(?:ing)?)\b",
    re.IGNORECASE,
)
_IMAGE_ROAST_RE = re.compile(
    r"\b(?:roast|cook|flame|drag|clown|shit\s*talk|talk\s+shit|make\s+fun\s+of)\b",
    re.IGNORECASE,
)
_IMAGE_OPINION_RE = re.compile(
    r"\b(?:what\s+(?:do|d'you|dya|u)\s+(?:you|u)?\s*think|thoughts?|opinion|"
    r"vibe\s*check|rate\s+(?:this|him|her|that|the\s+(?:guy|person|fit))|"
    r"what\s+about\s+(?:this|that)\s+(?:guy|person|fit))\b",
    re.IGNORECASE,
)
_IMAGE_BRIEF_RE = re.compile(
    r"\b(?:brief|quick|short|concise|super\s+concise|one[-\s]?liner)\b",
    re.IGNORECASE,
)
_IMAGE_ATL_STYLE_RE = re.compile(
    r"\b(?:atl|atlanta)\b",
    re.IGNORECASE,
)


def _clean_prompt(prompt: str, default: str) -> str:
    cleaned = re.sub(r"\s+", " ", (prompt or "").strip(" :-\n\t"))
    if not cleaned:
        cleaned = default
    if len(cleaned) > _MAX_PROMPT_CHARS:
        cleaned = cleaned[:_MAX_PROMPT_CHARS].rstrip()
    return cleaned


def _strip_direct_address(text: str) -> str:
    return _DIRECT_ADDRESS_RE.sub("", text or "", count=1).strip()


def _clean_generation_prompt(prompt: str, default: str = "") -> str:
    cleaned = _clean_prompt(prompt, default)
    cleaned = re.sub(
        r"\b(?:using|via|with|through|on)\s+(?:(?:google\s+)?gemini|nano\s*banana|openai|gpt|local|flux|sdxl)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-")
    return cleaned


def _clean_scan_prompt(prompt: str) -> str:
    cleaned = _clean_prompt(prompt, _DEFAULT_SCAN_PROMPT)
    normalized = cleaned.lower().strip(" .?!:;-")
    if not re.search(r"[a-z0-9]", normalized):
        return _DEFAULT_SCAN_PROMPT
    if re.fullmatch(rf"(?:this\s+)?{_IMAGE_NOUN}", normalized, flags=re.IGNORECASE):
        return _DEFAULT_SCAN_PROMPT
    if _IMAGE_ROAST_RE.search(cleaned) and not (_FOOD_ROAST_RE.search(cleaned) or _IMAGE_ROAST_NEGATION_RE.search(cleaned)):
        style = (
            "Roast this image in 1-3 short lines. Be sharp, funny, and specific to visible details. "
            "Roast the look/vibe, not identity or immutable traits. No disclaimers."
        )
        if _IMAGE_ATL_STYLE_RE.search(cleaned):
            style += " Use the ATL/Atlanta persona style: punchy, chaotic group-chat roast energy."
        return f"{style} User ask: {cleaned}"
    if _IMAGE_OPINION_RE.search(cleaned):
        return (
            "Give a quick opinion on this image in 1-2 short sentences. "
            f"Be casual and specific; skip the full image audit. User ask: {cleaned}"
        )
    if _IMAGE_BRIEF_RE.search(cleaned):
        return (
            "Answer this image request briefly in 1-3 short sentences. "
            f"Skip exhaustive description unless the user asks for detail. User ask: {cleaned}"
        )
    return cleaned


def _clean_attached_image_scan_prompt(prompt: str) -> str:
    cleaned = _clean_scan_prompt(prompt)
    normalized = cleaned.lower().strip(" .?!:;-")
    if _VAGUE_ATTACHED_IMAGE_SCAN_RE.fullmatch(normalized):
        return _DEFAULT_SCAN_PROMPT
    return cleaned


def _looks_like_attached_image_scan_request(text: str) -> bool:
    return bool(_ATTACHED_IMAGE_SCAN_CONTEXT_RE.search(text or ""))


def _generation_prompt_for_provider(prompt: str, provider: str) -> str:
    cleaned = _clean_prompt(prompt, "a clean minimal AI assistant logo concept")
    cleaned = re.sub(r"\bvia\s+nano\s*banana\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bnano\s*banana\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-")
    if not cleaned:
        cleaned = "a clean minimal AI assistant logo concept"
    lowered = cleaned.lower()
    if re.search(r"\bdavos\s*bot\b|\bdavosbot\b", lowered):
        cleaned = f"{cleaned}. {_DAVOSBOT_IMAGE_BRIEF}"

    if provider == "gemini":
        return (
            "Generate an image. Return image output, not a text-only reply. "
            f"Image brief: {cleaned}"
        )
    return cleaned


def _normalized_provider(provider: str, *, default: str = "auto") -> str:
    provider = (provider or default).strip().lower()
    return provider if provider in _VALID_IMAGE_PROVIDERS else default


def _normalized_scan_provider(provider: str, *, default: str = "auto") -> str:
    provider = (provider or default).strip().lower()
    return provider if provider in _VALID_SCAN_PROVIDERS else default


def _local_available() -> bool:
    return bool(LOCAL_IMAGE_ENDPOINT)


def _gemini_available() -> bool:
    return bool(GEMINI_API_KEY)


def _openai_available() -> bool:
    return bool(OPENAI_API_KEY)


def _openai_scan_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_VISION_MODEL)


def _openai_generation_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_IMAGE_MODEL)


def choose_generation_provider() -> str:
    provider = _normalized_provider(IMAGE_PROVIDER)
    if provider != "auto":
        return provider
    if _local_available():
        return "local"
    if _gemini_available():
        return "gemini"
    return "disabled"


def _auto_generation_providers() -> list[str]:
    providers: list[str] = []
    if _local_available():
        providers.append("local")
    if _gemini_available():
        providers.append("gemini")
    return providers or ["disabled"]


def choose_scan_provider() -> str:
    provider = _normalized_scan_provider(IMAGE_SCAN_PROVIDER)
    if provider != "auto":
        return provider
    if _gemini_available():
        return "gemini"
    return "disabled"


def estimate_generation_time(provider: str | None = None) -> str:
    provider = provider or choose_generation_provider()
    if provider == "local":
        return "about 2-4 minutes"
    if provider == "gemini":
        return "about 20-60 seconds"
    if provider == "openai":
        return "about 30-90 seconds"
    return "not available right now"


def estimate_scan_time(provider: str | None = None) -> str:
    provider = provider or choose_scan_provider()
    if provider in ("gemini", "openai"):
        return "about 10-30 seconds"
    return "not available right now"


def image_provider_status() -> str:
    generation = choose_generation_provider()
    scan = choose_scan_provider()
    gen_state = "available" if generation != "disabled" else "not configured"
    scan_state = "available" if scan != "disabled" else "not configured"
    return "\n".join([
        "Image routing:",
        f"  Generation: {gen_state} via {generation} (configured: {IMAGE_PROVIDER})",
        f"  Scan/read: {scan_state} via {scan} (configured: {IMAGE_SCAN_PROVIDER})",
        f"  Local image worker: {'configured' if _local_available() else 'not configured'}; model label: {LOCAL_IMAGE_MODEL}",
        f"  Gemini key: {'configured' if _gemini_available() else 'not configured'}; image model: {GEMINI_IMAGE_MODEL}",
        f"  Nano Banana: {NANO_BANANA_IMAGE_MODEL}; output {NANO_BANANA_IMAGE_SIZE} {NANO_BANANA_IMAGE_ASPECT_RATIO}; explicit only; 2K uses google-genai SDK when installed.",
        f"  OpenAI legacy: {'configured' if (_openai_scan_configured() or _openai_generation_configured()) else 'not configured'}; not used by auto routes.",
        "  Note: `gpt scan image` is legacy wording. It uses the configured scan provider, usually Gemini if OpenAI is not configured.",
        "  Image reads need an attached or recently buffered image in the same chat.",
    ])


def parse_openai_image_intent(text: str, has_image: bool = False) -> OpenAIImageIntent | None:
    """Recognize image requests while preserving the user's full scan question."""
    text = (text or "").strip()
    if not text:
        return None
    text = _strip_direct_address(text)
    if not text:
        return None

    for pattern in (_SCAN_IMAGE_RE, _SCAN_GPT_RE):
        match = pattern.match(text)
        if match:
            prompt = _clean_scan_prompt(match.group("prompt"))
            return OpenAIImageIntent("scan", prompt)

    if has_image:
        match = _REFERENCE_GENERATION_WITH_ATTACHED_IMAGE_RE.match(text)
        if match:
            prompt = _clean_generation_prompt(text, "make a new image based on the attached reference image")
            return OpenAIImageIntent("generate", prompt)

        match = _SCAN_WITH_ATTACHED_IMAGE_RE.match(text)
        if match:
            # The matched words carry intent too: 'who is this?' and 'read this'
            # must not become the same empty generic analysis prompt.
            prompt = _clean_scan_prompt(text)
            return OpenAIImageIntent("scan", prompt)

        if _looks_like_attached_image_scan_request(text):
            return OpenAIImageIntent("scan", _clean_attached_image_scan_prompt(text))

    match = _NANO_BANANA_GENERATION_RE.match(text)
    if match:
        prompt = _clean_generation_prompt(match.group("prompt"), "")
        if prompt:
            return OpenAIImageIntent("generate", prompt)

    match = _GENERATION_RE.match(text)
    if match:
        prompt = _clean_generation_prompt(match.group("prompt"), "")
        if prompt:
            return OpenAIImageIntent("generate", prompt)

    return None


def _openai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


def _safe_http_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("error", {}).get("message") or str(body)[:240]
    except Exception:
        detail = resp.text[:240]
    return redact_secret(detail)


def _safe_gemini_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("error", {}).get("message") or str(body)[:240]
    except Exception:
        detail = resp.text[:240]
    return redact_secret(detail)


def validate_image_path(image_path: str) -> tuple[bool, str, str]:
    """Validate a local image path for scan routes without exposing the path."""
    if not image_path:
        return False, "no image path was provided", ""
    try:
        path = Path(os.path.expanduser(image_path))
        if not path.exists():
            return False, "image file not found or not downloaded from iCloud yet", ""
        if not path.is_file():
            return False, "attachment path is not a file", ""
        size = path.stat().st_size
    except OSError:
        return False, "image file could not be read", ""
    if size <= 0:
        return False, "image file is empty", ""
    if size > _MAX_IMAGE_BYTES:
        return False, "image is too large for scan", ""

    suffix = path.suffix.lower()
    mime = _KNOWN_IMAGE_MIME_TYPES.get(suffix) or mimetypes.guess_type(path.name)[0] or ""
    if not mime.startswith("image/"):
        return False, "attachment does not look like an image", mime
    return True, "", mime


def _image_data_url(image_path: str) -> str:
    ok, reason, mime = validate_image_path(image_path)
    if not ok:
        raise ValueError(reason)
    path = Path(os.path.expanduser(image_path))
    if not path.exists():
        raise ValueError("image file not found")
    with path.open("rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _gemini_image_part(image_path: str) -> dict:
    ok, reason, mime = validate_image_path(image_path)
    if not ok:
        raise ValueError(reason)
    path = Path(os.path.expanduser(image_path))
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"inline_data": {"mime_type": mime, "data": encoded}}


def _extract_response_text(data: dict) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    for item in data.get("output", []) or []:
        for part in item.get("content", []) or []:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _extract_gemini_parts(data: dict) -> list[dict]:
    try:
        return data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return []


def _extract_gemini_text(data: dict) -> str:
    texts = []
    for part in _extract_gemini_parts(data):
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n".join(texts).strip()


def _log_gemini_image_usage(data: dict, source: str) -> None:
    usage = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
    log_gemini_usage(
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
        usage.get("totalTokenCount", 0),
        source,
    )


def _extract_gemini_image(data: dict) -> tuple[str, str]:
    for part in _extract_gemini_parts(data):
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict):
            b64 = inline.get("data")
            if isinstance(b64, str) and b64:
                return b64, inline.get("mimeType") or inline.get("mime_type") or "image/png"
    return "", "image/png"


def _extension_from_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get((mime or "").lower(), ".png")


def _write_generated_image(provider: str, b64: str, mime: str = "image/png") -> str:
    return _write_generated_image_bytes(provider, base64.b64decode(b64), mime)


def _write_generated_image_bytes(provider: str, image_bytes: bytes, mime: str = "image/png") -> str:
    out_dir = Path(OPENAI_IMAGE_OUTPUT_DIR)
    if not out_dir.is_absolute():
        out_dir = Path(PROJECT_ROOT) / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _extension_from_mime(mime)
    out_path = out_dir / f"{provider}_image_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
    out_path.write_bytes(image_bytes)
    return str(out_path)


def scan_openai_image(image_path: str, prompt: str) -> OpenAIImageResult:
    if not _openai_scan_configured():
        return OpenAIImageResult(
            False,
            "OpenAI image scan is not configured. Set OPENAI_API_KEY and OPENAI_VISION_MODEL on the Mini first.",
        )
    try:
        data_url = _image_data_url(image_path)
    except ValueError as exc:
        return OpenAIImageResult(False, f"OpenAI image scan skipped: {exc}.")

    payload = {
        "model": OPENAI_VISION_MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "max_output_tokens": 600,
    }
    try:
        logger.info("OpenAI image scan request: model=%s prompt_len=%d", OPENAI_VISION_MODEL, len(prompt))
        resp = requests.post(_OPENAI_RESPONSES_URL, headers=_openai_headers(), json=payload, timeout=60)
        if resp.status_code >= 400:
            return OpenAIImageResult(
                False,
                f"OpenAI image scan failed ({resp.status_code}): {_safe_http_error(resp)}",
                api_called=True,
            )
        text = _extract_response_text(resp.json())
        if not text:
            return OpenAIImageResult(False, "OpenAI image scan returned empty text.", api_called=True, provider="openai")
        return OpenAIImageResult(True, text, api_called=True, provider="openai")
    except requests.exceptions.Timeout:
        return OpenAIImageResult(False, "OpenAI image scan timed out.", api_called=True, provider="openai")
    except requests.exceptions.RequestException as exc:
        return OpenAIImageResult(False, f"OpenAI image scan failed: {redact_secret(str(exc))}", api_called=True, provider="openai")
    except Exception as exc:
        logger.exception("OpenAI image scan parse failure")
        return OpenAIImageResult(False, f"OpenAI image scan broke locally: {type(exc).__name__}.", api_called=True, provider="openai")


def scan_gemini_image(image_path: str, prompt: str) -> OpenAIImageResult:
    if not GEMINI_API_KEY:
        return OpenAIImageResult(False, "Gemini image scan is not configured.", provider="gemini")
    budget = check_gemini_budget("gemini_image_scan")
    if not budget.allowed:
        return OpenAIImageResult(False, "I can't do that right now. Gemini image reading is disabled by the spend guard.", provider="gemini")
    try:
        image_part = _gemini_image_part(image_path)
    except ValueError as exc:
        return OpenAIImageResult(False, f"Gemini image scan skipped: {exc}.", provider="gemini")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}, image_part],
            }
        ]
    }
    url = _gemini_generate_url(GEMINI_IMAGE_MODEL)
    try:
        logger.info("Gemini image scan request: model=%s prompt_len=%d", GEMINI_IMAGE_MODEL, len(prompt))
        resp = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=60)
        if resp.status_code >= 400:
            return OpenAIImageResult(
                False,
                f"Gemini image scan failed ({resp.status_code}): {_safe_gemini_error(resp)}",
                api_called=True,
                provider="gemini",
            )
        data = resp.json()
        _log_gemini_image_usage(data, "gemini_image_scan")
        text = _extract_gemini_text(data)
        if not text:
            return OpenAIImageResult(False, "Gemini image scan returned empty text.", api_called=True, provider="gemini")
        return OpenAIImageResult(True, text, api_called=True, provider="gemini")
    except requests.exceptions.Timeout:
        return OpenAIImageResult(False, "Gemini image scan timed out.", api_called=True, provider="gemini")
    except requests.exceptions.RequestException as exc:
        return OpenAIImageResult(False, f"Gemini image scan failed: {redact_secret(str(exc))}", api_called=True, provider="gemini")
    except Exception as exc:
        logger.exception("Gemini image scan parse failure")
        return OpenAIImageResult(False, f"Gemini image scan broke locally: {type(exc).__name__}.", api_called=True, provider="gemini")


def scan_image(image_path: str, prompt: str) -> OpenAIImageResult:
    provider = choose_scan_provider()
    if provider == "disabled":
        return OpenAIImageResult(False, "image reading is disabled.", provider="disabled")
    if provider == "gemini":
        return scan_gemini_image(image_path, prompt)
    if provider == "openai":
        return scan_openai_image(image_path, prompt)
    return OpenAIImageResult(False, "I can't do that right now. Image reading provider is invalid.")


def _extract_generated_b64(data: dict) -> str:
    for item in data.get("data", []) or []:
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            return b64
    for item in data.get("output", []) or []:
        if item.get("type") == "image_generation_call" and item.get("result"):
            return item["result"]
    return ""


def _gemini_generate_url(model: str, api_version: str | None = None) -> str:
    version = (api_version or GEMINI_IMAGE_API_VERSION or "v1").strip().strip("/") or "v1"
    return _GEMINI_GENERATE_URL_TEMPLATE.format(api_version=version, model=model)


def _gemini_image_generation_config(image_size: str | None = None, aspect_ratio: str | None = None) -> dict:
    config: dict[str, object] = {}
    image_config: dict[str, str] = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        config["responseFormat"] = {"image": image_config}
    return config


def _gemini_config_rejected(resp: requests.Response) -> bool:
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except Exception:
        detail = resp.text or ""
    return bool(
        resp.status_code == 400
        and "Unknown name" in detail
        and (
            "responseFormat" in detail
            or "responseModalities" in detail
            or "imageConfig" in detail
        )
    )


def _log_gemini_sdk_usage(usage: object, source: str) -> None:
    if usage is None:
        return
    log_gemini_usage(
        int(getattr(usage, "prompt_token_count", 0) or 0),
        int(getattr(usage, "candidates_token_count", 0) or 0),
        int(getattr(usage, "total_token_count", 0) or 0),
        source,
    )


def _extract_gemini_sdk_text(response: object) -> str:
    text = getattr(response, "text", "")
    if isinstance(text, str) and text.strip():
        return text.strip()
    texts: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", "")
            if isinstance(part_text, str) and part_text.strip():
                texts.append(part_text.strip())
    return "\n".join(texts).strip()


def _extract_gemini_sdk_image(response: object) -> tuple[bytes, str]:
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                if isinstance(data, str):
                    try:
                        return base64.b64decode(data), getattr(inline, "mime_type", "") or "image/png"
                    except (TypeError, ValueError):
                        return data.encode("utf-8"), getattr(inline, "mime_type", "") or "image/png"
                return bytes(data), getattr(inline, "mime_type", "") or "image/png"
    return b"", "image/png"


def _generate_gemini_image_via_sdk(
    provider_prompt: str,
    *,
    target_model: str,
    image_size: str | None,
    aspect_ratio: str | None,
    output_prefix: str,
    source: str,
) -> OpenAIImageResult | None:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Google GenAI SDK import failed, using REST fallback: %s", type(exc).__name__)
        return None

    try:
        config = None
        if image_size or aspect_ratio:
            image_config = types.ImageConfig(
                aspect_ratio=aspect_ratio or None,
                image_size=image_size or None,
            )
            config = types.GenerateContentConfig(image_config=image_config)
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=target_model,
            contents=[provider_prompt],
            config=config,
        )
        _log_gemini_sdk_usage(getattr(response, "usage_metadata", None), source)
        image_bytes, mime = _extract_gemini_sdk_image(response)
        if not image_bytes:
            text = _extract_gemini_sdk_text(response)
            note = f" Gemini said: {text[:160]}" if text else ""
            return OpenAIImageResult(False, f"Gemini image generation returned no image data.{note}", api_called=True, provider="gemini")
        out_path = _write_generated_image_bytes(output_prefix, image_bytes, mime)
        return OpenAIImageResult(True, "Gemini image generated.", out_path, api_called=True, provider="gemini")
    except Exception as exc:
        logger.warning("Google GenAI SDK image generation failed, using REST fallback: %s", redact_secret(str(exc))[:240])
        return None


def generate_gemini_image(
    prompt: str,
    *,
    model: str | None = None,
    image_size: str | None = None,
    aspect_ratio: str | None = None,
    output_prefix: str = "gemini",
    source: str = "gemini_image_generation",
) -> OpenAIImageResult:
    if not GEMINI_API_KEY:
        return OpenAIImageResult(False, "Gemini image generation is not configured.", provider="gemini")
    budget = check_gemini_budget(source)
    if not budget.allowed:
        return OpenAIImageResult(False, "I can't do that right now. Gemini image generation is disabled by the spend guard.", provider="gemini")
    target_model = (model or GEMINI_IMAGE_MODEL).strip() or GEMINI_IMAGE_MODEL
    provider_prompt = _generation_prompt_for_provider(prompt, "gemini")
    sdk_result = _generate_gemini_image_via_sdk(
        provider_prompt,
        target_model=target_model,
        image_size=image_size,
        aspect_ratio=aspect_ratio,
        output_prefix=output_prefix,
        source=source,
    )
    if sdk_result is not None:
        return sdk_result

    generation_config = _gemini_image_generation_config(image_size, aspect_ratio)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": provider_prompt}],
            }
        ]
    }
    if generation_config:
        payload["generationConfig"] = generation_config
    url = _gemini_generate_url(target_model)
    try:
        logger.info(
            "Gemini image generation request: model=%s size=%s aspect=%s prompt_len=%d",
            target_model,
            image_size or "",
            aspect_ratio or "",
            len(provider_prompt),
        )
        resp = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=180)
        config_fallback = False
        if "generationConfig" in payload and _gemini_config_rejected(resp):
            logger.warning(
                "Gemini image generation config rejected for model=%s; retrying default image generation",
                target_model,
            )
            fallback_payload = {"contents": payload["contents"]}
            resp = requests.post(url, params={"key": GEMINI_API_KEY}, json=fallback_payload, timeout=180)
            config_fallback = True
        if resp.status_code >= 400:
            return OpenAIImageResult(
                False,
                f"Gemini image generation failed ({resp.status_code}): {_safe_gemini_error(resp)}",
                api_called=True,
                provider="gemini",
            )
        data = resp.json()
        _log_gemini_image_usage(data, source)
        b64, mime = _extract_gemini_image(data)
        if not b64:
            text = _extract_gemini_text(data)
            note = f" Gemini said: {text[:160]}" if text else ""
            return OpenAIImageResult(False, f"Gemini image generation returned no image data.{note}", api_called=True, provider="gemini")
        out_path = _write_generated_image(output_prefix, b64, mime)
        message = "Gemini image generated."
        if config_fallback:
            message = "Gemini image generated at default size after Google rejected image size/aspect config."
        return OpenAIImageResult(True, message, out_path, api_called=True, provider="gemini")
    except requests.exceptions.Timeout:
        return OpenAIImageResult(False, "Gemini image generation timed out.", api_called=True, provider="gemini")
    except requests.exceptions.RequestException as exc:
        return OpenAIImageResult(False, f"Gemini image generation failed: {redact_secret(str(exc))}", api_called=True, provider="gemini")
    except Exception as exc:
        logger.exception("Gemini image generation parse/write failure")
        return OpenAIImageResult(False, f"Gemini image generation broke locally: {type(exc).__name__}.", api_called=True, provider="gemini")


def generate_nano_banana_image(prompt: str) -> OpenAIImageResult:
    return generate_gemini_image(
        prompt,
        model=NANO_BANANA_IMAGE_MODEL,
        image_size=NANO_BANANA_IMAGE_SIZE,
        aspect_ratio=NANO_BANANA_IMAGE_ASPECT_RATIO,
        output_prefix="nano_banana",
        source="gemini_nano_banana_image_generation",
    )


def generate_local_image(prompt: str) -> OpenAIImageResult:
    if not LOCAL_IMAGE_ENDPOINT:
        return OpenAIImageResult(False, "Local image generation is not configured.", provider="local")
    provider_prompt = _generation_prompt_for_provider(prompt, "local")
    payload = {"prompt": provider_prompt, "size": OPENAI_IMAGE_SIZE, "provider": "local"}
    try:
        logger.info("Local image generation request: endpoint=configured prompt_len=%d", len(provider_prompt))
        resp = requests.post(LOCAL_IMAGE_ENDPOINT, json=payload, timeout=LOCAL_IMAGE_TIMEOUT)
        if resp.status_code >= 400:
            return OpenAIImageResult(
                False,
                f"Local image generation failed ({resp.status_code}): {_safe_http_error(resp)}",
                api_called=True,
                provider="local",
            )
        data = resp.json()
        image_path = data.get("image_path") or data.get("path")
        if image_path and Path(image_path).exists():
            return OpenAIImageResult(True, "Local image generated.", image_path, api_called=True, provider="local")
        b64 = data.get("b64_json") or data.get("image_base64")
        if isinstance(b64, str) and b64:
            out_path = _write_generated_image("local", b64, data.get("mime_type", "image/png"))
            return OpenAIImageResult(True, "Local image generated.", out_path, api_called=True, provider="local")
        return OpenAIImageResult(False, "Local image generation returned no image data.", api_called=True, provider="local")
    except requests.exceptions.Timeout:
        return OpenAIImageResult(False, "Local image generation timed out.", api_called=True, provider="local")
    except requests.exceptions.RequestException as exc:
        return OpenAIImageResult(False, f"Local image generation failed: {redact_secret(str(exc))}", api_called=True, provider="local")
    except Exception as exc:
        logger.exception("Local image generation parse/write failure")
        return OpenAIImageResult(False, f"Local image generation broke locally: {type(exc).__name__}.", api_called=True, provider="local")


def generate_openai_image(prompt: str) -> OpenAIImageResult:
    if not _openai_generation_configured():
        return OpenAIImageResult(
            False,
            "OpenAI image feature is not configured. Set OPENAI_API_KEY and OPENAI_IMAGE_MODEL on the Mini first.",
            provider="openai",
        )

    payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
    }
    if OPENAI_IMAGE_SIZE:
        payload["size"] = OPENAI_IMAGE_SIZE
    if OPENAI_IMAGE_QUALITY:
        payload["quality"] = OPENAI_IMAGE_QUALITY

    try:
        logger.info(
            "OpenAI image generation request: model=%s size=%s quality=%s prompt_len=%d",
            OPENAI_IMAGE_MODEL,
            OPENAI_IMAGE_SIZE,
            OPENAI_IMAGE_QUALITY,
            len(prompt),
        )
        resp = requests.post(_OPENAI_IMAGE_GENERATIONS_URL, headers=_openai_headers(), json=payload, timeout=120)
        if resp.status_code >= 400:
            return OpenAIImageResult(
                False,
                f"OpenAI image generation failed ({resp.status_code}): {_safe_http_error(resp)}",
                api_called=True,
                provider="openai",
            )
        b64 = _extract_generated_b64(resp.json())
        if not b64:
            return OpenAIImageResult(False, "OpenAI image generation returned no image data.", api_called=True, provider="openai")

        out_path = _write_generated_image("openai", b64)
        return OpenAIImageResult(True, "OpenAI image generated.", out_path, api_called=True, provider="openai")
    except requests.exceptions.Timeout:
        return OpenAIImageResult(False, "OpenAI image generation timed out.", api_called=True, provider="openai")
    except requests.exceptions.RequestException as exc:
        return OpenAIImageResult(False, f"OpenAI image generation failed: {redact_secret(str(exc))}", api_called=True, provider="openai")
    except Exception as exc:
        logger.exception("OpenAI image generation parse/write failure")
        return OpenAIImageResult(False, f"OpenAI image generation broke locally: {type(exc).__name__}.", api_called=True, provider="openai")


def generate_image(prompt: str) -> OpenAIImageResult:
    configured_provider = _normalized_provider(IMAGE_PROVIDER)
    provider = choose_generation_provider()
    if provider == "disabled":
        return OpenAIImageResult(False, "image generation is disabled.", provider="disabled")
    if configured_provider == "auto":
        last_result: OpenAIImageResult | None = None
        for candidate in _auto_generation_providers():
            if candidate == "local":
                last_result = generate_local_image(prompt)
            elif candidate == "gemini":
                last_result = generate_gemini_image(prompt)
            elif candidate == "openai":
                last_result = generate_openai_image(prompt)
            else:
                last_result = OpenAIImageResult(False, "image generation is disabled.", provider="disabled")
            if last_result.ok:
                return last_result
            logger.warning("Image generation provider %s failed in auto mode: %s", candidate, last_result.message)
        return last_result or OpenAIImageResult(False, "image generation is disabled.", provider="disabled")
    if provider == "local":
        return generate_local_image(prompt)
    if provider == "gemini":
        return generate_gemini_image(prompt)
    if provider == "openai":
        return generate_openai_image(prompt)
    return OpenAIImageResult(False, "image generation is disabled.", provider="disabled")
