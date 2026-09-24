"""Native discovery copy explains real limits without performing the actions."""
import unittest
from unittest.mock import patch

from davosbot import commands, memory
import test_native_command_history as fixture


class CapabilityDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.NativeCommandHistoryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_capabilities_are_native_role_aware_and_available_to_followup(self):
        for sender in (fixture.OWNER, fixture.ADMIN, fixture.FRIEND):
            for group in (False, True):
                with self.subTest(sender=sender, group=group):
                    self.f.clear_history()
                    chat = fixture.GROUP if group else None
                    prefix = '@Davos ' if group else ''
                    self.f.route(prefix+'capabilities', sender=sender, chat=chat)
                    self.f.model.assert_not_called()
                    self.f.send.assert_called_once()
                    reply = self.f.send.call_args.args[1]
                    self.assertIn('weekly public research/waiver reports', reply)
                    self.assertIn('what permissions do I have?', reply)
                    self.assertIn('trigger/reply templates, not new code or model training', reply)
                    self.assertNotIn('create_skill', reply)
                    self.assertEqual(fixture.GROUP if group else sender, self.f.send.call_args.args[0])
                    self.assertEqual(reply, memory.get_history(chat or sender)[-1]['content'])
                    self.f.route(prefix+'Explain that option.', sender=sender, chat=chat)
                    self.assertEqual(reply, self.f.model.call_args.args[1][-1]['content'])

    def test_cron_help_is_an_owner_read_only_guide_not_a_creation_request(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.f.clear_history()
                self.f.route(('@Davos ' if group else '')+'cron help', chat=fixture.GROUP if group else None)
                self.f.model.assert_not_called()
                reply = self.f.send.call_args.args[1]
                for phrase in ('all times Pacific', 'same sender', 'within 5 minutes',
                               'bare yes never creates', 'list all crons` privately',
                               'not proof a report was generated or delivered',
                               'other types remain the owner-only'):
                    self.assertIn(phrase, reply)
                self.assertNotIn('Saved new', reply)
                self.assertNotIn('password gate', reply)

    def test_help_copy_has_no_side_effect_api(self):
        # These two production functions are pure copy/provider-status reads.
        with patch.object(commands, 'create_skill') as create, \
             patch.object(commands, '_cmd_confirmed_safe_cleanup') as cleanup:
            commands._cmd_cron_help()
            commands._cmd_capabilities(fixture.OWNER)
            create.assert_not_called()
            cleanup.assert_not_called()

    def test_group_cron_guide_is_complete_query_and_existing_admin_scope_only(self):
        for sender in (fixture.OWNER, fixture.ADMIN):
            for request in ('cron help', 'crons help?', 'JOBS HELP!'):
                with self.subTest(sender=sender, request=request):
                    self.f.clear_history()
                    self.f.route('@Davos '+request, sender=sender, chat=fixture.GROUP)
                    self.f.model.assert_not_called()
                    self.assertIn('Cron setup and controls', self.f.send.call_args.args[1])
        for request in ('@Davos cron help me delete #1', '@Davos explain cron help', '@Davos "cron help"'):
            with self.subTest(rejected=request):
                self.assertIsNone(commands.handle_group_command(fixture.OWNER, fixture.GROUP, request))
        self.assertIsNone(commands.handle_group_command(fixture.FRIEND, fixture.GROUP, '@Davos cron help'))


if __name__ == '__main__':
    unittest.main()
