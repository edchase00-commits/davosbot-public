import unittest

from scripts import check_repo_cleanliness


class RepoCleanlinessTests(unittest.TestCase):
    def test_blocks_private_runtime_files(self):
        errors = check_repo_cleanliness.check_paths([
            ".env",
            "MEMORY.md",
            "exports/private/change_log_board.md",
            "generated/images/test.png",
            "davosbot.db",
        ])

        self.assertGreaterEqual(len(errors), 5)
        self.assertTrue(any(".env" in error for error in errors))
        self.assertTrue(any("exports/private" in error for error in errors))

    def test_blocks_root_runtime_modules(self):
        errors = check_repo_cleanliness.check_paths(["tools.py", "brain.py", "main.py", "davosbot/tools.py"])

        self.assertTrue(any("tools.py" in error for error in errors))
        self.assertTrue(any("brain.py" in error for error in errors))
        self.assertFalse(any(error.startswith("main.py:") for error in errors))
        self.assertFalse(any("davosbot/tools.py" in error for error in errors))

    def test_allows_public_templates_and_examples(self):
        errors = check_repo_cleanliness.check_paths([
            ".env.example",
            "MEMORY.example.md",
            "SOUL.example.md",
            "gc_state.example.json",
            "personalities/example.md",
            "davosbot/commands.py",
        ])

        self.assertEqual([], errors)

    def test_blocks_local_personas(self):
        errors = check_repo_cleanliness.check_paths(["personalities/private.md"])

        self.assertTrue(any("local persona" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
