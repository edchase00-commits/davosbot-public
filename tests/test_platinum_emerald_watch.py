import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from davosbot import platinum_emerald_watch as watch


MANIFEST = {"game": "Platinum Emerald", "version": "20261030cg",
            "release": "full4_release_20261030cg",
            "output": {"bytes": 33554432, "sha1": "b" * 40}}
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
PREVIEW = "<title>Platinum Emerald: development preview</title><p>Status: development preview. It is still being worked on.</p>"
RELEASE = "<h1>Platinum Emerald</h1><p>Status: full release.</p>"


def inspect(html, manifest=None):
    return watch.inspect_release(html, manifest or MANIFEST, NOW.date())


class ReleaseEvidenceTests(unittest.TestCase):
    def test_current_preview_and_full4_manifest_do_not_trigger(self):
        self.assertEqual(inspect(PREVIEW)["events"], [])
        self.assertEqual(inspect(PREVIEW)["status"], "preview")

    def test_explicit_availability_and_ended_preview(self):
        result = inspect("<h1>Platinum Emerald</h1><p>The preview has ended. The full release is now available.</p>")
        self.assertEqual(result["events"][0]["key"], "full-release")

    def test_inline_markup_preserves_release_punctuation(self):
        result = inspect("<h1>Platinum Emerald</h1><p>Status: <b>full release</b>.</p>")
        self.assertEqual(result["events"][0]["key"], "full-release")

    def test_explicit_date_colon(self):
        result = inspect(PREVIEW + "<p>Full release date: November 15, 2026.</p>")
        self.assertEqual(result["events"][0]["key"], "date:2026-11-15")

    def test_active_preview_disclaimer_vetoes_availability(self):
        result = inspect(PREVIEW + "<p>The full release is now available.</p>")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["status"], "conflict")

    def test_removed_preview_disclaimer_does_not_establish_release(self):
        self.assertEqual(inspect("<h1>Platinum Emerald</h1>")["status"], "unconfirmed")

    def test_dates_can_be_announced_during_preview_without_claiming_release(self):
        result = inspect(PREVIEW + "<p>The full release launches November 15, 2026.</p>")
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["events"][0]["key"], "date:2026-11-15")

    def test_date_arrival_is_not_availability_and_past_announcement_is_ignored(self):
        html = "<h1>Platinum Emerald</h1><p>The full release launches 2026-10-30.</p>"
        self.assertEqual(inspect(html)["events"][0]["kind"], "date")
        self.assertEqual(watch.inspect_release(html, MANIFEST, date(2026, 10, 31))["events"], [])

    def test_unsupported_or_uncertain_prose_is_quiet(self):
        for text in ("The full release is not available now.", "The full release is coming soon.",
                     "The full release isn't available now.", "Status: full release candidate.",
                     "If the full release is out now, use the patcher.",
                     "We will announce the full release date on November 15, 2026.",
                     "The full release is coming soon. The next developer update is November 15, 2026.",
                     "The stable release is delayed until November 15, 2026.",
                     "We hope the full release launches November 15, 2026.",
                     "The full release launches November 15, 2026 or November 22, 2026."):
            with self.subTest(text=text):
                self.assertEqual(inspect("<h1>Platinum Emerald</h1><p>" + text + "</p>")["events"], [])

    def test_scripts_comments_and_metadata_cannot_announce(self):
        html = '<h1>Platinum Emerald</h1><script>Status: full release</script><!-- Status: full release --><meta content="full release is now available">'
        self.assertEqual(inspect(html)["events"], [])

    def test_hidden_elements_cannot_announce(self):
        for attributes in ('hidden', 'aria-hidden="true"', 'style="display: none"'):
            html = f'<h1>Platinum Emerald</h1><p><span {attributes}><span>Status: full release.</span> Status: full release.</span></p>'
            self.assertEqual(inspect(html)["events"], [])

    def test_bad_manifest_fails_closed(self):
        for change in ({"game": "Other"}, {"version": ""}, {"output": {}},
                       {"output": {"bytes": True, "sha1": "b" * 40}},
                       {"output": {"bytes": 33554433, "sha1": "b" * 40}},
                       {"output": {"bytes": 1, "sha1": "invalid"}}):
            with self.subTest(change=change), self.assertRaises(watch.WatchError):
                inspect(RELEASE, dict(MANIFEST, **change))

    def test_rejects_other_page(self):
        with self.assertRaises(watch.WatchError):
            inspect("<p>Status: full release</p>")


class DurableWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "watch.json"
        self.messages = []
        self.queue = Mock(side_effect=lambda message, due: self.messages.append(message))
        self.pending = lambda message: message in self.messages

    def process(self, observation):
        return watch.process_observation(self.path, observation, NOW, self.queue, self.pending)

    def test_preview_never_calls_reminder_helper(self):
        self.assertEqual(self.process(inspect(PREVIEW)), "unchanged")
        self.queue.assert_not_called()

    def test_matching_existing_native_reminder_is_not_duplicated(self):
        self.pending = lambda message: True
        self.assertEqual(self.process(inspect(RELEASE)), "queued")
        self.queue.assert_not_called()

    def test_release_is_once_even_after_new_build(self):
        self.assertEqual(self.process(inspect(RELEASE)), "queued")
        newer = copy.deepcopy(MANIFEST)
        newer["version"] = "20261101"
        newer["output"]["sha1"] = "a" * 40
        self.assertEqual(self.process(inspect(RELEASE, newer)), "unchanged")
        self.queue.assert_called_once()
        self.assertEqual(self.queue.call_args.args[1], "2026-10-30 18:02:00")

    def test_date_dedupes_normalized_date_and_notifies_reschedule(self):
        for day in ("November 15, 2026", "2026-11-15", "2026-11-22"):
            self.process(inspect(PREVIEW + f"<p>Full release launches {day}.</p>"))
        self.assertEqual(self.queue.call_count, 2)

    def test_claim_is_durable_before_helper_and_crash_is_not_replayed(self):
        def crash(*args):
            self.assertEqual(watch.load_state(self.path)["events"]["full-release"]["state"], "claimed")
            raise KeyboardInterrupt
        self.queue.side_effect = crash
        with self.assertRaises(KeyboardInterrupt):
            self.process(inspect(RELEASE))
        self.queue.side_effect = None
        self.assertEqual(self.process(inspect(RELEASE)), "needs_review")
        self.queue.assert_called_once()

    def test_unverified_helper_outcome_is_not_retried(self):
        self.queue.side_effect = None
        self.assertEqual(self.process(inspect(RELEASE)), "needs_review")
        self.assertEqual(self.process(inspect(RELEASE)), "needs_review")
        self.queue.assert_called_once()
        self.assertEqual(watch.load_state(self.path)["events"]["full-release"]["state"], "ambiguous")

    def test_ambiguous_claim_stays_visible_after_page_changes(self):
        self.queue.side_effect = None
        self.process(inspect(RELEASE))
        self.assertEqual(self.process(inspect(PREVIEW)), "needs_review")
        self.queue.assert_called_once()

    def test_existing_claim_can_reconcile_from_native_pending_list(self):
        def crash_after_queue(message, due):
            self.messages.append(message)
            raise KeyboardInterrupt
        self.queue.side_effect = crash_after_queue
        with self.assertRaises(KeyboardInterrupt):
            self.process(inspect(RELEASE))
        self.assertEqual(self.process(inspect(RELEASE)), "unchanged")
        self.queue.assert_called_once()
        self.assertEqual(watch.load_state(self.path)["events"]["full-release"]["state"], "queued")

    def test_corrupt_state_never_resets_deduplication(self):
        self.path.write_text('{"schema":1,"events":[]}', encoding="utf-8")
        with self.assertRaises(watch.WatchError):
            self.process(inspect(RELEASE))
        self.queue.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Mini/POSIX service lock")
    def test_concurrent_runner_cannot_enter(self):
        lock = self.path.with_suffix(".lock")
        with watch.watch_lock(lock):
            with self.assertRaises(watch.WatchError):
                with watch.watch_lock(lock):
                    self.fail("second lock succeeded")

    def test_native_adapter_uses_only_configured_owner_and_reads_back(self):
        setter, lister = Mock(), Mock(return_value="Pending reminders:\n1. today \u2014 exact message")
        modules = {"davosbot.config": SimpleNamespace(OWNER_ID="synthetic-owner"),
                   "davosbot.reminder_tools": SimpleNamespace(_set_reminder=setter, _list_reminders=lister)}
        with patch.dict("sys.modules", modules):
            queue, pending = watch.native_reminders()
            queue("exact message", "2026-10-30 18:02:00")
            self.assertTrue(pending("exact message"))
            lister.return_value = "Pending reminders:\n1. today \u2014 quoted exact message"
            self.assertFalse(pending("exact message"))
            lister.return_value = "Pending reminders:\n1. today \u2014 quoted \u2014 exact message"
            self.assertFalse(pending("exact message"))
        setter.assert_called_once_with("exact message", "2026-10-30 18:02:00", originating_chat_id="synthetic-owner")
        self.assertEqual(lister.call_args.args, ("synthetic-owner",))

    def test_no_owner_fails_before_claim(self):
        modules = {"davosbot.config": SimpleNamespace(OWNER_ID=""),
                   "davosbot.reminder_tools": SimpleNamespace(_set_reminder=Mock(), _list_reminders=Mock())}
        with patch.dict("sys.modules", modules), self.assertRaises(watch.WatchError):
            watch.native_reminders()


if __name__ == "__main__":
    unittest.main()
