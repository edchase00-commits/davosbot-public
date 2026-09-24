#!/usr/bin/env python3
"""Prepare an explicitly selected local image; never upload or contact Davos."""

import argparse
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from davosbot.work_image_input import (MAX_IMAGE_BYTES, MAX_IMAGE_DIMENSION,
                                      MAX_IMAGE_PIXELS, _decoder)

MAX_LOCAL_BYTES = 16 * 1024 * 1024
MAX_LOCAL_PIXELS = 16 * 1024 * 1024


def prepare_image(path, *, maximum_dimension=None, jpeg_quality=None):
    if maximum_dimension is not None and (type(maximum_dimension) is not int or not 1 <= maximum_dimension <= 4096):
        raise ValueError("invalid_resize_dimension")
    if jpeg_quality is not None and (type(jpeg_quality) is not int or not 1 <= jpeg_quality <= 95):
        raise ValueError("invalid_jpeg_quality")
    Image, ImageOps = _decoder()
    with Path(path).open("rb") as source_file:
        raw = source_file.read(MAX_LOCAL_BYTES + 1)
    if not raw or len(raw) > MAX_LOCAL_BYTES:
        raise ValueError("local_image_size_limit")
    try:
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as source:
            if (getattr(source, "n_frames", 1) != 1 or source.width * source.height > MAX_LOCAL_PIXELS):
                raise ValueError("local_image_content_invalid")
            source.verify()
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as source:
            source.load()
            with ImageOps.exif_transpose(source) as upright:
                if maximum_dimension is not None:
                    upright.thumbnail((maximum_dimension, maximum_dimension), Image.Resampling.LANCZOS)
                width, height = upright.size
                if max(width, height) > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("explicit_resize_required")
                with upright.convert("RGBA") as rgba, Image.new("RGB", rgba.size, "white") as clean:
                    with rgba.getchannel("A") as alpha:
                        clean.paste(rgba, mask=alpha)
                    output = BytesIO()
                    if jpeg_quality is None:
                        clean.save(output, format="PNG")
                    else:
                        clean.save(output, format="JPEG", quality=jpeg_quality)
                    image_bytes = output.getvalue()
    except Exception as exc:
        code = str(exc) if str(exc) == "explicit_resize_required" else "local_image_content_invalid"
        raise ValueError(code) from None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("explicit_resize_or_jpeg_compression_required")
    return {
        "encoding": "base64", "content": base64.b64encode(image_bytes).decode("ascii"),
        "image_blob_sha": hashlib.sha1(b"blob " + str(len(image_bytes)).encode("ascii") + b"\0" + image_bytes).hexdigest(),
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "mime_type": "image/png" if jpeg_quality is None else "image/jpeg",
        "byte_count": len(image_bytes), "width": width, "height": height,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="exact local PNG/JPEG selected by the user")
    parser.add_argument("--output", type=Path, required=True, help="new private JSON file; existing files are never overwritten")
    parser.add_argument("--max-dimension", type=int, help="explicitly allow downsizing; never enlarge")
    parser.add_argument("--jpeg-quality", type=int, help="explicitly allow lossy JPEG compression (1..95); otherwise lossless PNG")
    args = parser.parse_args(argv)
    try:
        prepared = prepare_image(args.input, maximum_dimension=args.max_dimension, jpeg_quality=args.jpeg_quality)
        with args.output.open("x", encoding="utf-8") as output:
            os.chmod(args.output, 0o600)
            json.dump(prepared, output, sort_keys=True)
            output.write("\n")
    except FileExistsError:
        parser.exit(1, "Output already exists; choose a new private file.\n")
    except (OSError, ValueError) as exc:
        code = str(exc) if type(exc) is ValueError else "local_file_unavailable"
        parser.exit(1, "Image preparation failed: " + code + ". No upload was attempted.\n")
    print(f"Prepared {prepared['width']}x{prepared['height']} {prepared['mime_type']}, {prepared['byte_count']} bytes. No upload performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
