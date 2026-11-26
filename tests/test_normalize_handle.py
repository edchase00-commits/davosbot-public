import unittest

from davosbot.config import normalize_handle


class NormalizeHandleTests(unittest.TestCase):
    def test_us_numbers_normalize_to_e164(self):
        self.assertEqual("+13369700454", normalize_handle("336-970-0454"))
        self.assertEqual("+13369700454", normalize_handle("(336) 970-0454"))
        self.assertEqual("+13369700454", normalize_handle("1 336 970 0454"))
        self.assertEqual("+13369700454", normalize_handle("+1 (336) 970-0454"))

    def test_email_handles_lowercase(self):
        self.assertEqual("person@example.com", normalize_handle("Person@Example.COM"))

    def test_unrecognized_handles_are_preserved(self):
        self.assertEqual("abc123", normalize_handle("abc123"))
        self.assertEqual("12345", normalize_handle("12345"))


if __name__ == "__main__":
    unittest.main()
