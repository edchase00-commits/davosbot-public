import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_length_guard(is_owner_func):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "_NON_OWNER_TEXT_CHAR_LIMIT" for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_non_owner_length_rejection":
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"is_owner": is_owner_func}
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace["_non_owner_length_rejection"], namespace["_NON_OWNER_TEXT_CHAR_LIMIT"]


class AccessSafetyTests(unittest.TestCase):
    def test_non_owner_giant_messages_are_rejected_before_llm(self):
        guard, limit = _load_length_guard(lambda sender: sender == "owner")

        self.assertIsNone(guard("friend", "x" * limit))
        reply = guard("friend", "x" * (limit + 1))

        self.assertIn("too long", reply)
        self.assertIn("non-owner", reply)

    def test_owner_giant_messages_are_allowed(self):
        guard, limit = _load_length_guard(lambda sender: sender == "owner")

        self.assertIsNone(guard("owner", "x" * (limit + 10000)))


if __name__ == "__main__":
    unittest.main()
