"""Synthetic-only fixtures for a fixed-file, owner-only Messages declaration probe."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from davosbot import messages_capabilities as probe
from davosbot import work_actions as actions, work_actions_extra as extra


OWNER = "+15550000001"
OTHER = "+15550000002"
SDEF = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd">
<dictionary title="synthetic private title"><suite name="fixture" code="test">
  <command name="send" code="testsend"/>
  <command name="make" code="corecrel"/>
  <class name="chat" code="icha"><responds-to command="make"/></class>
  <class name="application" code="capp"><element type="chat"/></class>
</suite></dictionary>'''


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                     and hasattr(os, "mkfifo"), "fixed descriptor reads require POSIX")
class MessagesCapabilityProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.system = root / "System" / "Applications" / "Messages.app"
        self.legacy = root / "Applications" / "Messages.app"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(probe, "_BUNDLES", (("system", self.system), ("applications", self.legacy))))
        stack.enter_context(patch.object(probe.sys, "platform", "darwin"))
        self.no_process = stack.enter_context(patch("subprocess.run", side_effect=AssertionError("no processes")))
        self.no_system = stack.enter_context(patch("os.system", side_effect=AssertionError("no shell")))

    def bundle(self, *, root=None, definition="Messages.sdef", dictionary=SDEF, metadata=None):
        root = root or self.system
        resources = root / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        info = {"CFBundleIdentifier": "com.apple.iChat", "CFBundleShortVersionString": "14.0",
                "CFBundleVersion": "1400.1.2", "OSAScriptingDefinition": definition,
                "UnusedPrivateValue": "synthetic_private_value"}
        info.update(metadata or {})
        (root / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        if probe._safe_sdef(definition) and dictionary is not None:
            (resources / definition).write_bytes(dictionary)
        return root

    def observe(self):
        result = probe.probe_messages_capabilities()
        self.assertEqual("unknown", result["group_creation_works"])
        self.assertEqual("unknown", result["sending_works"])
        self.assertLess(len(json.dumps(result).encode()), 7000)
        self.no_process.assert_not_called()
        self.no_system.assert_not_called()
        return result

    def assert_unknown(self, reason):
        result = self.observe()
        self.assertEqual("unknown", result["probe_state"])
        self.assertEqual(reason, result["reason"])
        self.assertTrue(all(flag is None for flag in result["declarations"].values()))
        return result

    def test_reads_version_and_literal_declarations_without_returning_raw_data(self):
        self.bundle()
        result = self.observe()
        self.assertEqual("observed", result["probe_state"])
        self.assertEqual("declarations_only", result["reason"])
        self.assertEqual("14.0", result["app_version"])
        self.assertEqual("1400.1.2", result["app_build"])
        self.assertTrue(all(result["declarations"].values()))
        for forbidden in ("synthetic", str(self.system), "com.apple", "chat.db"):
            self.assertNotIn(forbidden, json.dumps(result))
        self.assertEqual({"schema_version", "source", "probe_state", "reason", "bundle_location",
                          "metadata_shape", "bundle_identity", "definition_metadata_shape", "definition_basename",
                          "definition_name_state", "definition_source", "resources_scan_status", "unsafe_candidate_names_present",
                          "sdef_candidates", "app_version", "app_build", "declarations",
                          "group_creation_works", "sending_works"}, set(result))

    def test_bounded_identity_evidence_accepts_only_reviewed_messages_ids(self):
        cases = [
            ({"CFBundleIdentifier": "com.apple.iChat"}, "dictionary", "legacy_ichat", "observed"),
            ({"CFBundleIdentifier": "com.apple.MobileSMS"}, "dictionary", "mobile_sms", "observed"),
            ({"CFBundleIdentifier": "private.untrusted.identifier"}, "dictionary", "other", "unknown"),
            ({"CFBundleIdentifier": ["private.untrusted.identifier"]}, "dictionary", "other", "unknown"),
            ({}, "dictionary", "missing", "unknown"),
            (["private.untrusted.identifier"], "other", "unknown", "unknown"),
        ]
        for info, shape, identity, state in cases:
            with self.subTest(shape=shape, identity=identity):
                self.bundle()
                if isinstance(info, dict):
                    info = {"OSAScriptingDefinition": "Messages.sdef", **info}
                (self.system / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
                with patch.object(probe, "_parse_dictionary", wraps=probe._parse_dictionary) as parse:
                    result = self.observe()
                    self.assertEqual(shape, result["metadata_shape"])
                    self.assertEqual(identity, result["bundle_identity"])
                    self.assertEqual(state, result["probe_state"])
                    self.assertNotIn("private.untrusted.identifier", json.dumps(result))
                    if identity not in ("legacy_ichat", "mobile_sms"):
                        self.assertEqual("unrecognized_bundle", result["reason"])
                        self.assertTrue(all(v is None for v in result["declarations"].values()))
                        self.assertIsNone(result["app_version"])
                        parse.assert_not_called()

    def test_both_known_ids_keep_fixed_dictionary_and_file_guards(self):
        for identifier in ("com.apple.iChat", "com.apple.MobileSMS"):
            with self.subTest(identifier=identifier):
                self.bundle(metadata={"CFBundleIdentifier": identifier})
                result = self.observe()
                self.assertEqual("system", result["bundle_location"])
                self.assertEqual("observed", result["probe_state"])
                self.assertTrue(all(result["declarations"].values()))
                self.bundle(metadata={"CFBundleIdentifier": identifier}, definition="../private.sdef")
                self.assert_unknown("unsupported_definition_location")
                self.bundle(metadata={"CFBundleIdentifier": identifier}, dictionary=b"<!DOCTYPE dictionary [<!ENTITY x 'private'>]><dictionary/>")
                self.assert_unknown("unsupported_dictionary")
                self.bundle(metadata={"CFBundleIdentifier": identifier})
                definition = self.system / "Contents" / "Resources" / "Messages.sdef"
                definition.unlink()
                definition.symlink_to(self.system / "Contents" / "Info.plist")
                self.assert_unknown("dictionary_unreadable")
                definition.unlink()

    def test_malformed_metadata_has_no_inferred_identity(self):
        self.bundle()
        (self.system / "Contents" / "Info.plist").write_bytes(b"malformed private metadata")
        result = self.assert_unknown("invalid_bundle_metadata")
        self.assertEqual("unknown", result["metadata_shape"])
        self.assertEqual("unknown", result["bundle_identity"])

    def test_fixed_legacy_location_and_allowlisted_legacy_definition(self):
        self.bundle(root=self.legacy, definition="iChat.sdef")
        self.assertEqual("applications", self.observe()["bundle_location"])

    def test_missing_bundles_are_unknown(self):
        self.assert_unknown("bundle_not_found")

    def test_other_platform_never_reads_any_file(self):
        with patch.object(probe.sys, "platform", "linux"), patch.object(probe, "_read_at") as reader:
            self.assert_unknown("unsupported_platform")
        reader.assert_not_called()

    def test_bundle_identity_must_match(self):
        self.bundle(metadata={"CFBundleIdentifier": "synthetic.other"})
        self.assert_unknown("unrecognized_bundle")

    def test_invalid_plist_is_unknown(self):
        root = self.bundle()
        for data in (b"not a plist", plistlib.dumps(["wrong shape"])):
            with self.subTest(data=data):
                (root / "Contents" / "Info.plist").write_bytes(data)
                self.assert_unknown("invalid_bundle_metadata" if data == b"not a plist" else "unrecognized_bundle")

    def test_missing_or_unsafe_dictionary_names_are_never_followed(self):
        for definition in ("../../private.sdef", "/tmp/private.sdef", "nested/Messages.sdef", "", 1):
            with self.subTest(definition=definition):
                self.bundle(metadata={"OSAScriptingDefinition": definition})
                result = self.assert_unknown("unsupported_definition_location")
                if isinstance(definition, str) and definition:
                    self.assertNotIn(definition, json.dumps(result))
        root = self.bundle()
        path = root / "Contents" / "Info.plist"
        info = plistlib.loads(path.read_bytes())
        del info["OSAScriptingDefinition"]
        path.write_bytes(plistlib.dumps(info))
        self.assert_unknown("unsupported_definition_location")

    def test_missing_dictionary_is_unknown(self):
        self.bundle(dictionary=None)
        self.assert_unknown("dictionary_not_found")

    def test_permission_denial_is_fixed_unknown_with_no_raw_error_or_fallback(self):
        self.bundle()
        with patch.object(probe, "_read_at", side_effect=PermissionError("synthetic private path")) as reader:
            result = self.assert_unknown("bundle_unreadable")
        self.assertEqual(1, reader.call_count)
        self.assertNotIn("synthetic", json.dumps(result))

    def test_dictionary_permission_denial_is_unknown(self):
        self.bundle()
        original = probe._read_at
        def read(directory, name, maximum):
            if name.endswith(".sdef"):
                raise PermissionError("private")
            return original(directory, name, maximum)
        with patch.object(probe, "_read_at", side_effect=read):
            self.assert_unknown("dictionary_unreadable")

    def test_bad_system_bundle_does_not_fall_through_to_other_install(self):
        self.bundle(metadata={"CFBundleIdentifier": "other"})
        self.bundle(root=self.legacy)
        self.assert_unknown("unrecognized_bundle")

    def test_oversized_dictionary_and_plist_are_unknown(self):
        root = self.bundle(dictionary=b" " * (probe._MAX_DICTIONARY_BYTES + 1))
        self.assert_unknown("file_bounds")
        (root / "Contents" / "Info.plist").write_bytes(b" " * (probe._MAX_PLIST_BYTES + 1))
        self.assert_unknown("file_bounds")

    def test_symlink_file_and_directory_cannot_read_outside_bundle(self):
        root = self.bundle()
        resources = root / "Contents" / "Resources"
        dictionary = resources / "Messages.sdef"
        dictionary.unlink()
        dictionary.symlink_to(root / "Contents" / "Info.plist")
        self.assert_unknown("dictionary_unreadable")
        dictionary.unlink()
        resources.rmdir()
        resources.symlink_to(root / "Contents", target_is_directory=True)
        self.assert_unknown("dictionary_unreadable")

    def test_nonregular_file_does_not_block(self):
        root = self.bundle()
        dictionary = root / "Contents" / "Resources" / "Messages.sdef"
        dictionary.unlink()
        os.mkfifo(dictionary)
        self.assert_unknown("file_bounds")

    def test_invalid_and_unresolved_dictionaries_are_unknown(self):
        cases = (
            (b"<broken", "invalid_dictionary"),
            (b"\xff", "invalid_dictionary"),
            (b"<other/>", "invalid_dictionary"),
            (b"<dictionary/>", "invalid_dictionary"),
            (b'<dictionary xmlns:xi="http://www.w3.org/2001/XInclude"><suite/><xi:include href="file:///private"/></dictionary>', "unsupported_dictionary"),
            (b'<!DOCTYPE dictionary SYSTEM "https://example.invalid/private"><dictionary><suite/></dictionary>', "unsupported_dictionary"),
            (b'<!DOCTYPE dictionary [<!ENTITY secret SYSTEM "file:///private">]><dictionary><suite/>&secret;</dictionary>', "unsupported_dictionary"),
            (b'<!DOCTYPE dictionary [<!ENTITY x "EXPANSION">]><dictionary><suite/>&x;</dictionary>', "unsupported_dictionary"),
        )
        for data, reason in cases:
            with self.subTest(data=data):
                self.bundle(dictionary=data)
                self.assert_unknown(reason)

    def test_absent_literal_flags_do_not_establish_runtime_support(self):
        self.bundle(dictionary=b'<dictionary><suite name="empty"/></dictionary>')
        result = self.observe()
        self.assertEqual("observed", result["probe_state"])
        self.assertTrue(all(value is False for value in result["declarations"].values()))

    def test_generic_make_does_not_imply_chat_creation(self):
        self.bundle(dictionary=b'<dictionary><suite><command name="make"/></suite></dictionary>')
        flags = self.observe()["declarations"]
        self.assertTrue(flags["make_command_declared"])
        self.assertFalse(flags["chat_make_response_declared"])

    def test_class_extensions_are_literal_declarations_not_inferred_inheritance(self):
        self.bundle(dictionary=b'''<dictionary><suite>
          <class name="chat" inherits="item"/>
          <class-extension extends="chat"><responds-to command="make"/></class-extension>
          <class-extension extends="application"><element type="chat"/></class-extension>
        </suite></dictionary>''')
        flags = self.observe()["declarations"]
        self.assertTrue(flags["chat_make_response_declared"])
        self.assertTrue(flags["application_chat_element_declared"])
        self.assertFalse(flags["make_command_declared"])

    def test_unknown_version_formats_are_not_echoed(self):
        for version in ("private@example.invalid", "1\nprivate", "1" * 41, "", 12):
            with self.subTest(version=version):
                self.bundle(metadata={"CFBundleShortVersionString": version, "CFBundleVersion": version})
                result = self.observe()
                self.assertIsNone(result["app_version"])
                self.assertIsNone(result["app_build"])

    def test_arbitrary_safe_declared_sdef_is_confined_and_observed(self):
        self.bundle(definition='Actual Messages 26.sdef')
        result = self.observe()
        self.assertEqual(2, result['schema_version'])
        self.assertEqual('string', result['definition_metadata_shape'])
        self.assertEqual('safe_basename', result['definition_name_state'])
        self.assertEqual('Actual Messages 26.sdef', result['definition_basename'])
        self.assertEqual('declared', result['definition_source'])
        self.assertEqual('observed', result['probe_state'])
        self.assertEqual('declared', result['sdef_candidates'][0]['source'])

    def test_metadata_shapes_are_bounded_without_echoing_values(self):
        from datetime import datetime
        for value, shape in [(True, 'boolean'), (7, 'integer'), (1.5, 'real'),
                             (['private'], 'array'), ({'private': 'private'}, 'dictionary'),
                             (b'private', 'data'), (datetime(2026, 1, 1), 'date')]:
            with self.subTest(shape=shape):
                self.bundle(metadata={'OSAScriptingDefinition': value})
                result = self.observe()
                self.assertEqual(shape, result['definition_metadata_shape'])
                self.assertEqual('not_string', result['definition_name_state'])
                self.assertIsNone(result['definition_basename'])
                self.assertEqual('none', result['definition_source'])
                self.assertNotIn('private', json.dumps(result))
                self.assertTrue(all(v is None for v in result['declarations'].values()))

    def test_unsafe_names_are_never_used_as_read_paths(self):
        for name in ['../Secrets.sdef', '/private/x.sdef', 'dir/file.sdef', 'dir\\file.sdef',
                     'file://x.sdef', '$(x).sdef', '~/x.sdef', '.hidden.sdef', 'x\x00.sdef',
                     'x\n.sdef', 'é.sdef', 'x' * 81 + '.sdef', ' x.sdef', 'x.sdef ', 'x%2fs.sdef']:
            with self.subTest(name=name):
                self.bundle()
                info_path = self.system / "Contents" / "Info.plist"
                info = plistlib.loads(info_path.read_bytes())
                info["OSAScriptingDefinition"] = name
                info_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
                reader = probe._read_at
                seen = []
                def record(directory, actual, maximum):
                    seen.append(actual)
                    return reader(directory, actual, maximum)
                with patch.object(probe, '_read_at', side_effect=record):
                    result = self.observe()
                self.assertEqual('unsafe_name', result['definition_name_state'])
                self.assertIsNone(result['definition_basename'])
                self.assertNotIn(name, seen)
                self.assertTrue(all('/' not in item and '\\' not in item for item in seen))

    def test_missing_empty_and_safe_non_sdef_are_distinct(self):
        for value, state, basename in [('', 'empty', None), ('MessagesDictionary', 'safe_basename', 'MessagesDictionary')]:
            self.bundle(definition=value)
            result = self.observe()
            self.assertEqual(state, result['definition_name_state'])
            self.assertEqual(basename, result['definition_basename'])
            self.assertEqual('none', result['definition_source'])
        self.bundle()
        info_path = self.system / 'Contents' / 'Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        del info['OSAScriptingDefinition']
        info_path.write_bytes(plistlib.dumps(info))
        result = self.observe()
        self.assertEqual('missing', result['definition_metadata_shape'])
        self.assertEqual('none', result['definition_source'])
        self.assertEqual('candidate', result['sdef_candidates'][0]['source'])
        self.assertTrue(result['sdef_candidates'][0]['declarations']['chat_class_declared'])
        self.assertTrue(all(v is None for v in result['declarations'].values()))

    def test_declared_failure_does_not_adopt_discovered_dictionary(self):
        self.bundle(definition='Missing.sdef', dictionary=None)
        resources = self.system / 'Contents' / 'Resources'
        (resources / 'Other.sdef').write_bytes(SDEF)
        result = self.observe()
        self.assertEqual('dictionary_not_found', result['reason'])
        self.assertEqual('unknown', result['probe_state'])
        self.assertEqual(['declared', 'candidate'], [x['source'] for x in result['sdef_candidates']])
        self.assertEqual('observed', result['sdef_candidates'][1]['state'])
        self.assertTrue(all(v is None for v in result['declarations'].values()))

    def test_conflicting_candidates_do_not_merge_with_declared_flags(self):
        self.bundle(dictionary=b'<dictionary><suite><command name="send"/></suite></dictionary>')
        resources = self.system / 'Contents' / 'Resources'
        (resources / 'Other.sdef').write_bytes(SDEF)
        result = self.observe()
        self.assertTrue(result['declarations']['send_command_declared'])
        self.assertFalse(result['declarations']['chat_class_declared'])
        self.assertTrue(result['sdef_candidates'][1]['declarations']['chat_class_declared'])

    def test_no_recursion_unrelated_files_or_unsafe_names_returned(self):
        self.bundle()
        resources = self.system / 'Contents' / 'Resources'
        (resources / 'Localizations').mkdir()
        (resources / 'Localizations' / 'Nested.sdef').write_bytes(SDEF)
        (resources / 'private.txt').write_text('private body')
        (resources / '.private.sdef').write_bytes(SDEF)
        result = self.observe()
        self.assertEqual(['Messages.sdef'], [x['basename'] for x in result['sdef_candidates']])
        self.assertTrue(result['unsafe_candidate_names_present'])
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('Nested', json.dumps(result))

    def test_candidate_limit_is_explicit_and_read_attempts_are_bounded(self):
        self.bundle()
        resources = self.system / 'Contents' / 'Resources'
        for number in range(10):
            (resources / f'Candidate{number}.sdef').write_bytes(SDEF)
        with patch.object(probe, '_inspect_definition', wraps=probe._inspect_definition) as inspect:
            result = self.observe()
        self.assertEqual('candidate_limit', result['resources_scan_status'])
        self.assertLessEqual(inspect.call_count, 8)
        self.assertLessEqual(len(result['sdef_candidates']), 8)
        self.assertEqual('observed', result['probe_state'])

    def test_entry_limit_never_means_no_dictionary(self):
        self.bundle()
        with patch.object(probe, '_MAX_RESOURCE_ENTRIES', 0):
            result = self.observe()
        self.assertEqual('entry_limit', result['resources_scan_status'])
        self.assertEqual('declared', result['definition_source'])
        self.assertEqual('observed', result['probe_state'])

    def test_declared_and_candidate_share_one_eight_read_budget(self):
        self.bundle(definition='Missing.sdef', dictionary=None)
        resources = self.system / 'Contents' / 'Resources'
        for number in range(8):
            (resources / f'Candidate{number}.sdef').write_bytes(SDEF)
        with patch.object(probe, '_inspect_definition', wraps=probe._inspect_definition) as inspect:
            result = self.observe()
        self.assertEqual(8, inspect.call_count)
        self.assertEqual('candidate_limit', result['resources_scan_status'])
        self.assertEqual('dictionary_not_found', result['reason'])

    def test_bundle_and_resources_renames_do_not_redirect_pinned_reads(self):
        self.bundle()
        contents = self.system / 'Contents'
        original_read = probe._read_at
        swapped = False
        def swap_contents(directory, name, maximum):
            nonlocal swapped
            raw = original_read(directory, name, maximum)
            if name == 'Info.plist' and not swapped:
                contents.rename(self.system / 'PreviousContents')
                replacement = contents / 'Resources'
                replacement.mkdir(parents=True)
                (replacement / 'Messages.sdef').write_bytes(b'<dictionary><suite/></dictionary>')
                swapped = True
            return raw
        original_scan = probe._scan_definition_names
        def swap_resources(directory):
            result = original_scan(directory)
            old = self.system / 'PreviousContents' / 'Resources'
            old.rename(self.system / 'PreviousContents' / 'PreviousResources')
            old.mkdir()
            (old / 'Messages.sdef').write_bytes(b'<dictionary><suite/></dictionary>')
            return result
        with patch.object(probe, '_read_at', side_effect=swap_contents), patch.object(probe, '_scan_definition_names', side_effect=swap_resources):
            result = self.observe()
        self.assertTrue(result['declarations']['chat_class_declared'])

    def test_existing_bundle_missing_contents_or_plist_never_falls_back(self):
        self.system.mkdir(parents=True)
        self.bundle(root=self.legacy)
        self.assert_unknown('bundle_unreadable')
        (self.system / 'Contents').mkdir()
        self.assert_unknown('bundle_unreadable')

    def test_maximal_candidate_output_fits_transport_without_truncation(self):
        self.bundle(definition='Absent.sdef', dictionary=None)
        resources = self.system / 'Contents' / 'Resources'
        for number in range(7):
            (resources / (str(number) + 'x' * 74 + '.sdef')).write_bytes(SDEF)
        result = self.observe()
        self.assertEqual(8, len(result['sdef_candidates']))
        self.assertLess(len(json.dumps({'status': 'ok', 'evidence': result}).encode()), 7800)


    def test_growing_file_is_bounded_after_initial_stat(self):
        self.bundle()
        path = self.system / 'Contents' / 'Resources' / 'Messages.sdef'
        inode = path.stat().st_ino
        original = os.fstat
        grown = False
        def stat_then_grow(fd):
            nonlocal grown
            result = original(fd)
            if result.st_ino == inode and not grown:
                with path.open('ab') as output:
                    output.write(b' ' * (probe._MAX_DICTIONARY_BYTES + 1))
                grown = True
            return result
        with patch.object(probe.os, 'fstat', side_effect=stat_then_grow):
            self.assert_unknown('file_bounds')

    def test_real_work_adapter_preserves_bounded_candidate_evidence(self):
        self.bundle(definition='Missing.sdef', dictionary=None)
        resources = self.system / 'Contents' / 'Resources'
        for number in range(7):
            (resources / (str(number) + 'x' * 74 + '.sdef')).write_bytes(SDEF)
        modules = {'config': SimpleNamespace(OWNER_ID=OWNER),
                   'permissions': SimpleNamespace(is_owner=lambda actor: actor == OWNER),
                   'messages_capabilities': probe}
        with patch.object(extra, '_module', side_effect=lambda name: modules[name]), \
             patch('davosbot.config.OWNER_ID', OWNER), patch('davosbot.permissions.OWNER_ID', OWNER):
            result = actions.execute_action('messages.capabilities', {}, owner=OWNER)
        self.assertEqual('ok', result['status'])
        evidence = result['evidence']
        self.assertEqual(8, len(evidence['sdef_candidates']))
        self.assertEqual('dictionary_not_found', evidence['reason'])
        self.assertTrue(evidence['sdef_candidates'][1]['declarations']['chat_class_declared'])
        self.assertNotIn('[truncated]', json.dumps(result))
        self.assertLess(len(json.dumps(result).encode()), 7800)



class MessagesCapabilityWorkPermissionTests(unittest.TestCase):
    def setUp(self):
        self.reader = Mock(return_value={"probe_state": "unknown", "group_creation_works": "unknown"})
        self.modules = {"config": SimpleNamespace(OWNER_ID=OWNER),
                        "permissions": SimpleNamespace(is_owner=lambda actor: actor == OWNER),
                        "messages_capabilities": SimpleNamespace(probe_messages_capabilities=self.reader)}
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.lookup = stack.enter_context(patch.object(extra, "_module", side_effect=lambda name: self.modules[name]))
        stack.enter_context(patch("davosbot.config.OWNER_ID", OWNER))
        stack.enter_context(patch("davosbot.permissions.OWNER_ID", OWNER))

    def execute(self, args=None, owner=OWNER):
        return actions.execute_action("messages.capabilities", {} if args is None else args, owner=owner)

    def test_catalogue_declares_read_only_empty_args_without_probe(self):
        spec = actions.action_catalogue()["messages.capabilities"]
        self.assertEqual({}, spec["fields"])
        self.assertIs(False, spec["mutates"])
        self.assertEqual("dictionary_declarations_only", spec["evidence_scope"])
        actions.validate_action("messages.capabilities", {})
        self.reader.assert_not_called()
        self.lookup.assert_not_called()

    def test_authenticated_action_calls_only_fixed_probe(self):
        result = self.execute()
        self.assertEqual("ok", result["status"])
        self.reader.assert_called_once_with()
        self.assertEqual(["config", "permissions", "messages_capabilities"],
                         [call.args[0] for call in self.lookup.call_args_list])

    def test_nonowner_cannot_read_bundle(self):
        for owner in (OTHER, "", False):
            with self.subTest(owner=owner):
                self.assertEqual("error", self.execute(owner=owner)["status"])
        self.reader.assert_not_called()
        self.lookup.assert_not_called()

    def test_native_owner_gate_is_preserved(self):
        self.modules["permissions"].is_owner = lambda actor: False
        self.assertEqual("error", self.execute()["status"])
        self.reader.assert_not_called()

    def test_all_caller_arguments_and_wrong_shapes_are_rejected_before_reads(self):
        for args in ({"path": "/private"}, {"command": "send"}, {"chat_id": "a" * 32},
                     {"sender": OWNER}, {"recipient": OTHER}, {"permission": True},
                     {"sql": "select 1"}, {"app": "Messages"}, {"execute": False}, [], "", False):
            with self.subTest(args=args):
                self.assertEqual("error", self.execute(args)["status"])
        self.reader.assert_not_called()
        self.lookup.assert_not_called()

    def test_unsupported_names_never_become_general_execution(self):
        for name in ("messages.send", "messages.create_group", "messages.script", "messages.read"):
            with self.subTest(name=name):
                self.assertEqual("error", actions.execute_action(name, {}, owner=OWNER)["status"])
        self.reader.assert_not_called()
        self.lookup.assert_not_called()

    def test_probe_errors_are_not_raw_exceptions_or_retry_authority(self):
        self.reader.side_effect = RuntimeError("synthetic_private_value")
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertIs(False, result["evidence"]["ambiguous"])
        self.assertNotIn("synthetic_private_value", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
