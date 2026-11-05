"""Execute only the argument-selection snippet, never a cleanup job or model."""
import json,os,subprocess,unittest
from pathlib import Path

class ModelSelectionTests(unittest.TestCase):
 def args(self,value):
  text=(Path(__file__).resolve().parents[1]/'scripts/nightly_safe_cleanup_codex.sh').read_text()
  snippet=text[text.index('CODEX_CLEANUP_MODEL_ARGS=()'):text.index('\n"$CODEX_BIN"',text.index('CODEX_CLEANUP_MODEL_ARGS=()'))]
  env=dict(os.environ);env.pop('CODEX_CLEANUP_MODEL',None)
  if value is not None:env['CODEX_CLEANUP_MODEL']=value
  # Only prints shell-array length/values. No source of the executable runner.
  command=snippet+'\nprintf "%s\\n" "${#CODEX_CLEANUP_MODEL_ARGS[@]}" "${CODEX_CLEANUP_MODEL_ARGS[@]}"'
  return subprocess.run(['/bin/bash','-c',command],env=env,check=True,capture_output=True,text=True).stdout.splitlines()
 def test_default_and_empty_inherit_verified_config(self):
  self.assertEqual(self.args(None)[0],'0');self.assertEqual(self.args('')[0],'0')
 def test_explicit_override_is_one_literal_argument(self):
  self.assertEqual(self.args('verified-test-model')[:3],['2','--model','verified-test-model'])
 def test_no_shell_expansion_of_override(self):
  self.assertEqual(self.args('$(false); test model')[2],'$(false); test model')
 def test_no_retired_fallback_in_command(self):
  text=(Path(__file__).resolve().parents[1]/'scripts/nightly_safe_cleanup_codex.sh').read_text()
  self.assertNotIn('gpt-5.4',text);self.assertIn('"${CODEX_CLEANUP_MODEL_ARGS[@]}"',text)
