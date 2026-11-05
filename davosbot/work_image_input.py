"""Owner-only Work scans of bounded immutable image blobs, never URLs or paths.

The transport has authenticated the request and committed its attempt before
this adapter runs. Only the selected blob is fetched. Temporary provider input
contains decoded pixels, not uploaded metadata, and is never retained as chat
context. GitHub retains the uploaded original according to its own policies.
"""

import base64
import binascii
import hashlib
import hmac
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile


MAX_IMAGE_BYTES = 1_048_576
MAX_IMAGE_PIXELS = 4_194_304
MAX_IMAGE_DIMENSION = 4096
MAX_SANITIZED_BYTES = 16_777_216
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG"}


def _fail(code):
    raise ValueError(code)


def _decoder():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        _fail("image_decoder_unavailable")
    return Image, ImageOps


def _fetch_blob(blob_sha):
    if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
        _fail("image_blob_invalid")
    from .config import PROJECT_ROOT
    from .work_bridge import BridgeError, GitHubTransport, REPOSITORY

    transport = GitHubTransport(PROJECT_ROOT)
    try:
        # Recheck repository privacy/identity immediately before reading bytes.
        if not transport.assert_channel():
            _fail("image_channel_paused")
        return transport._call([f"repos/{REPOSITORY}/git/blobs/{blob_sha}", "--method", "GET"])
    except BridgeError:
        _fail("image_blob_unavailable")


def _blob_bytes(blob, args):
    if (not isinstance(blob, dict) or blob.get("sha") != args["image_blob_sha"]
            or blob.get("encoding") != "base64" or type(blob.get("size")) is not int
            or not 0 < blob["size"] <= MAX_IMAGE_BYTES):
        _fail("image_blob_invalid")
    encoded = blob.get("content")
    # GitHub may line-wrap base64. No other whitespace or alphabet is accepted.
    if not isinstance(encoded, str) or len(encoded) > 2 * MAX_IMAGE_BYTES:
        _fail("image_blob_invalid")
    encoded = encoded.replace("\n", "").replace("\r", "")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        _fail("image_blob_invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        _fail("image_blob_invalid")
    if (len(raw) != blob["size"] or base64.b64encode(raw).decode("ascii") != encoded
            or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), args["image_sha256"])):
        _fail("image_blob_mismatch")
    git_sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if not hmac.compare_digest(git_sha, args["image_blob_sha"]):
        _fail("image_blob_mismatch")
    return raw


def _sanitize_image(raw, mime_type, decoder):
    Image, ImageOps = decoder
    try:
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as source:
            width, height = source.size
            if (source.format != _FORMATS[mime_type] or getattr(source, "n_frames", 1) != 1
                    or not 0 < width <= MAX_IMAGE_DIMENSION
                    or not 0 < height <= MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS):
                _fail("image_content_invalid")
            source.verify()
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as source:
            source.load()  # Decode fully; truncated/corrupt data must not reach a provider.
            upright = ImageOps.exif_transpose(source)
            try:
                rgba = upright.convert("RGBA")
                try:
                    width, height = rgba.size
                    # A fresh raster discards EXIF, comments, ICC and text chunks.
                    clean = Image.new("RGB", rgba.size, "white")
                    try:
                        clean.paste(rgba, mask=rgba.getchannel("A"))
                        output = BytesIO()
                        clean.save(output, format="PNG")
                        sanitized = output.getvalue()
                    finally:
                        clean.close()
                finally:
                    rgba.close()
            finally:
                upright.close()
        if not sanitized or len(sanitized) > MAX_SANITIZED_BYTES:
            _fail("image_content_invalid")
        return sanitized, width, height
    except Exception:
        # Decoder exceptions can contain image metadata or input-derived text.
        _fail("image_content_invalid")


def scan_uploaded_image(args, owner):
    """Return a bounded scan result without a send, arbitrary file read or model tools."""
    from .config import OWNER_ID
    from .permissions import is_owner
    from .work_actions_extra import validate_extra_action

    validate_extra_action("images.scan", args)
    if not isinstance(owner, str) or not OWNER_ID or owner != OWNER_ID or not is_owner(owner):
        _fail("owner_required")
    from .image_access import image_access_denial
    from .memory import log_tool_use
    from .openai_images import OPENAI_IMAGE_SCAN_TOOL, scan_image, validate_image_path

    if image_access_denial(owner):
        _fail("image_access_denied")
    decoder = _decoder()
    raw = _blob_bytes(_fetch_blob(args["image_blob_sha"]), args)
    sanitized, width, height = _sanitize_image(raw, args["mime_type"], decoder)
    # No caller-controlled name, root or directory. TemporaryDirectory is private
    # on POSIX; the image is explicitly mode 0600. Never keep it as image history.
    with tempfile.TemporaryDirectory(prefix="davos-work-scan-") as temporary:
        image_path = Path(temporary) / "image.png"
        with image_path.open("xb") as image_file:
            os.chmod(image_path, 0o600)
            image_file.write(sanitized)
        valid, _reason, _mime = validate_image_path(str(image_path))
        if not valid:
            _fail("image_content_invalid")
        result = scan_image(str(image_path), args["question"])
        if result.api_called:
            # Same uncapped-owner per-attempt accounting as native image scans.
            log_tool_use(owner, OPENAI_IMAGE_SCAN_TOOL)
    provider = result.provider if result.provider in {"openai", "gemini", "disabled"} else "unknown"
    evidence = {"provider": provider, "api_called": bool(result.api_called),
                "width": width, "height": height, "input_bytes": len(raw),
                "sent": False, "temporary_input_retained": False}
    if not result.ok:
        # Provider failures may echo request bytes/metadata. Do not publish them.
        return {"status": "error", "result": "Image scan did not complete. Check the configured image provider and spend guard; no message was sent.",
                "evidence": {**evidence, "code": "image_scan_failed"}}
    return {"status": "ok", "result": result.message, "evidence": evidence}
