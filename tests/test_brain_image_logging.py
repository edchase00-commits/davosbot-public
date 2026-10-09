import ast
import base64
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _CaptureLogger:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(message % args if args else message)


def _load_image_part_helper():
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.FunctionDef))
        and (
            getattr(node, "name", "") == "_image_part"
            or any(getattr(target, "id", "") == "_IMAGE_MIME_MAP" for target in getattr(node, "targets", []))
        )
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    logger = _CaptureLogger()
    namespace = {"base64": base64, "os": os, "logger": logger}
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace["_image_part"], logger


class BrainImageLoggingTests(unittest.TestCase):
    def test_missing_image_warning_does_not_log_raw_path(self):
        image_part, logger = _load_image_part_helper()
        secret_path = "/tmp/private-attachment-secret-name.png"

        self.assertIsNone(image_part(secret_path))

        joined = "\n".join(logger.messages)
        self.assertIn("Failed to load image attachment", joined)
        self.assertIn("FileNotFoundError", joined)
        self.assertNotIn(secret_path, joined)
        self.assertNotIn("secret-name", joined)


if __name__ == "__main__":
    unittest.main()
