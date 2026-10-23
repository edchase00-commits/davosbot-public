import tempfile,unittest
from pathlib import Path
from scripts import scrub_public_runtime_bindings as scrubber,scan_public_snapshot as scanner
from scripts import integrate_codex_branch as integrator

PRIVATE='a1b2c3d4'*4
class BindingExportTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)/'snapshot';self.root.mkdir()
  (self.root/'davosbot').mkdir();(self.root/'davosbot/native_fitness.py').write_text("SCHEDULE = '"+PRIVATE+"'\n")
 def test_actual_binding_removed_everywhere_and_idempotent(self):
  (self.root/'tests').mkdir();p=self.root/'tests/fixture.py';p.write_text('value='+repr(PRIVATE))
  scrubber.scrub(self.root)
  self.assertNotIn(PRIVATE,p.read_text());self.assertFalse((self.root/'davosbot/native_fitness.py').exists())
  self.assertEqual(scrubber.scrub(self.root)['changed_files'],0);self.assertFalse(scanner.scan_snapshot(self.root))
 def test_artifacts_and_nested_bridge_removed(self):
  directory=self.root/'davosbot/.work_bridge';directory.mkdir();(directory/'state.json').write_text('{private activation data}')
  (self.root/'native_fitness_handoff.json').write_text('{private}')
  scrubber.scrub(self.root);self.assertFalse(directory.exists());self.assertFalse((self.root/'native_fitness_handoff.json').exists())
 def test_unknown_payload_fails_closed_even_in_tests(self):
  (self.root/'tests').mkdir();p=self.root/'tests/unsafe.py';p.write_text('x={"activation_id":"'+'b'*36+'"}')
  self.assertTrue(scanner.scan_snapshot(self.root))
  with self.assertRaises(ValueError):scrubber.scrub(self.root)
 def test_scanner_catches_literal_schedule_in_tests(self):
  (self.root/'tests').mkdir();(self.root/'tests/unsafe.py').write_text("SCHEDULE = '"+PRIVATE+"'")
  self.assertTrue(any('binding' in f.label for f in scanner.scan_snapshot(self.root)))
 def test_symlink_cannot_escape_destination(self):
  (self.root/'outside').symlink_to(self.root.parent/'outside')
  with self.assertRaises(ValueError):scrubber.scrub(self.root)
 def test_existing_git_checkout_rejected(self):
  (self.root/'.git').mkdir()
  with self.assertRaises(ValueError):scrubber.scrub(self.root)
 def test_native_authority_requires_manual_review_path(self):
  for path in ['davosbot/work_occurrences.py','davosbot/native_fitness.py','davosbot/native_fitness_worker.py','davosbot/native_workout_review.py','scripts/integrate_codex_branch.py']:
   self.assertTrue(integrator.red_tier_reason(path))
 def test_export_invokes_checked_gate(self):
  path=Path(__file__).resolve().parents[1]/'scripts/export_public_snapshot.ps1'
  if not path.exists():self.skipTest('Private exporter intentionally omitted from public snapshot')
  source=path.read_text()
  self.assertIn('scripts/scrub_public_runtime_bindings.py',source);self.assertIn('Public runtime-binding scrub failed',source)

 def test_private_feature_and_hooks_omitted_public_only(self):
  source=Path(__file__).resolve().parents[1]
  (self.root/'davosbot/main.py').write_text((source/'davosbot/main.py').read_text())
  (self.root/'davosbot/work_occurrences.py').write_text((source/'davosbot/work_occurrences.py').read_text())
  scrubber.scrub(self.root)
  text=(self.root/'davosbot/main.py').read_text();self.assertNotIn('native_fitness',text)
  namespace={};exec((self.root/'davosbot/work_occurrences.py').read_text(),namespace)
  with self.assertRaisesRegex(ValueError,'unavailable'):namespace['execute_native']()
 def test_wal_and_unexpected_binding_payload_gate(self):
  (self.root/'copy.sqlite3-wal').write_bytes(b'PRIVATE')
  scrubber.scrub(self.root);self.assertFalse((self.root/'copy.sqlite3-wal').exists())
  (self.root/'rogue.json').write_text('{'+chr(34)+'bindings'+chr(34)+':{}}')
  with self.assertRaises(ValueError):scrubber.scrub(self.root)
 def test_direct_cli_has_importable_scanner(self):
  import subprocess,sys
  script=Path(__file__).resolve().parents[1]/'scripts/scrub_public_runtime_bindings.py'
  result=subprocess.run([sys.executable,str(script),str(self.root)],cwd=self.root.parent,check=True,capture_output=True,text=True)
  self.assertIn('scrub passed',result.stdout)

 def test_binary_binding_fails_closed(self):
  (self.root/'asset.bin').write_bytes(b'\x00'+PRIVATE.encode())
  with self.assertRaisesRegex(ValueError,'bytes_remaining'):scrubber.scrub(self.root)
 def test_multiline_payload_fails_closed(self):
  (self.root/'rogue.json').write_text('{"bindings":\n {}}')
  with self.assertRaises(ValueError):scrubber.scrub(self.root)

class BudgetExportTests(unittest.TestCase):
 def test_budget_files_hooks_and_nested_state_removed(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'snapshot';(root/'davosbot').mkdir(parents=True);(root/'tests').mkdir()
   source=Path(__file__).resolve().parents[1]
   (root/'davosbot/main.py').write_text((source/'davosbot/main.py').read_text())
   for f in ['davosbot/budget_once.py','davosbot/budget_service.py','tests/test_budget_once.py','tests/test_budget_service.py']:(root/f).write_text('private synthetic fixture')
   state=root/'davosbot/.budget_once';state.mkdir();(state/'manifest.json').write_text('private synthetic body')
   scrubber.scrub(root)
   self.assertNotIn('budget_once',(root/'davosbot/main.py').read_text());self.assertNotIn('budget_service',(root/'davosbot/main.py').read_text())
   self.assertFalse(state.exists());self.assertFalse((root/'davosbot/budget_once.py').exists());self.assertFalse(any('budget' in finding.path for finding in scanner.scan_snapshot(root)))
 def test_budget_runtime_changes_require_manual_review(self):
  for p in ['davosbot/budget_once.py','davosbot/budget_service.py']:self.assertTrue(integrator.red_tier_reason(p))

 def test_standalone_scanner_rejects_nested_budget_state(self):
  for p in ['.budget_once/manifest.json','davosbot/.budget_once/claims.sqlite3','copy/nested/.budget_once/activation.json']:
   self.assertTrue(scanner.path_findings(p))

class RecurringExportTests(unittest.TestCase):
 def test_recurring_modules_state_and_tests_omitted(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)/'snapshot';(root/'davosbot').mkdir(parents=True);(root/'tests').mkdir()
   paths=['davosbot/budget_recurring_service.py','tests/test_budget_recurring_service.py','scripts/budget_live_workflow_validate.py','.github/workflows/budget-data-dispatch.yml','.github/workflows/budget-validator.yml','scripts/budget_workflow_validate.py','tests/test_budget_workflow_validate.py','davosbot/recurring_budget.py','davosbot/budget_validator_core.py','tests/test_recurring_budget.py']
   for p in paths:
    (root/p).parent.mkdir(parents=True,exist_ok=True)
    (root/p).write_text('private synthetic fixture')
   state=root/'davosbot/.budget_recurring';state.mkdir();(state/'activation.json').write_text('synthetic')
   scrubber.scrub(root)
   self.assertFalse(state.exists());self.assertTrue(all(not (root/p).exists() for p in paths))
   self.assertFalse(scanner.scan_snapshot(root))
 def test_nested_recurring_state_and_modules_require_private_review(self):
  self.assertTrue(scanner.path_findings('nested/.budget_recurring/occurrences.sqlite3'))
  for p in ['davosbot/budget_recurring_service.py','scripts/budget_live_workflow_validate.py','scripts/budget_workflow_validate.py','davosbot/recurring_budget.py','davosbot/budget_validator_core.py']:self.assertTrue(integrator.red_tier_reason(p))
