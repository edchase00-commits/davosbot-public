import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_priority_helpers():
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    names = {"_handle_priority_intake_command"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if "_PRIORITY_INTAKE_COMMAND_RE" in target_names:
                nodes.append(node)
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    calls = []
    namespace = {
        "re": re,
        "handle_command": lambda sender, text: calls.append((sender, text)) or "LOGGED",
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    namespace["calls"] = calls
    return namespace


class LogPriorityRoutingTests(unittest.TestCase):
    def test_log_payload_with_image_and_cron_words_gets_command_priority(self):
        helpers = _load_priority_helpers()
        handler = helpers["_handle_priority_intake_command"]
        text = (
            "Log Image read via gemini: the screenshot says the sports recap cron "
            "for Cole needs changes and the previous log did not update."
        )

        reply = handler("+15550000001", text)

        self.assertEqual("LOGGED", reply)
        self.assertEqual([("+15550000001", text)], helpers["calls"])

    def test_non_intake_cron_text_still_falls_through(self):
        helpers = _load_priority_helpers()
        handler = helpers["_handle_priority_intake_command"]

        reply = handler("+15550000001", "set up daily sports recap cron for Cole at 6pm")

        self.assertIsNone(reply)
        self.assertEqual([], helpers["calls"])

    def test_repair_intake_with_cron_words_gets_priority(self):
        helpers = _load_priority_helpers()
        handler = helpers["_handle_priority_intake_command"]
        text = "ship this cron fix"

        reply = handler("+15550000001", text)

        self.assertEqual("LOGGED", reply)
        self.assertEqual([("+15550000001", text)], helpers["calls"])

    def test_image_generation_failure_complaint_gets_priority(self):
        helpers = _load_priority_helpers()
        handler = helpers["_handle_priority_intake_command"]
        text = "Log that my image was never generated"

        reply = handler("+15550000001", text)

        self.assertEqual("LOGGED", reply)
        self.assertEqual([("+15550000001", text)], helpers["calls"])


if __name__ == "__main__":
    unittest.main()
