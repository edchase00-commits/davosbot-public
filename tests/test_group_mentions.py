import unittest

from davosbot.group_chat import is_at_mentioned, normalize_group_mention_command, strip_mention


class GroupMentionTests(unittest.TestCase):
    def test_explicit_at_mention_works_anywhere(self):
        self.assertTrue(is_at_mentioned("@Davos help"))
        self.assertEqual("help", strip_mention("@Davos help"))

        self.assertTrue(is_at_mentioned("I'm upset @davos"))
        self.assertEqual("I'm upset", strip_mention("I'm upset @davos"))
        self.assertEqual("@Davos I'm upset", normalize_group_mention_command("I'm upset @davos"))

    def test_plain_ios_mentions_are_conservative_direct_address(self):
        self.assertTrue(is_at_mentioned("Davos help"))
        self.assertEqual("help", strip_mention("Davos help"))
        self.assertEqual("@Davos help", normalize_group_mention_command("Davos help"))

        self.assertTrue(is_at_mentioned("hey Davos can you check this"))
        self.assertEqual("can you check this", strip_mention("hey Davos can you check this"))

        self.assertTrue(is_at_mentioned("computa make these guys super gay and horny"))
        self.assertEqual("make these guys super gay and horny", strip_mention("computa make these guys super gay and horny"))

        self.assertTrue(is_at_mentioned("I'm upset Davos"))
        self.assertEqual("I'm upset", strip_mention("I'm upset Davos"))

    def test_plain_davos_reference_does_not_trigger(self):
        self.assertFalse(is_at_mentioned("what is Davos?"))
        self.assertFalse(is_at_mentioned("Davos is a ski town"))
        self.assertFalse(is_at_mentioned("that Davos thing was funny yesterday"))
        self.assertFalse(is_at_mentioned("email me at test@davos.com"))


if __name__ == "__main__":
    unittest.main()
