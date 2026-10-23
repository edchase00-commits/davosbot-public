"""Deterministic prompt inspection only: no model generation or message sends."""
import unittest
from unittest.mock import patch
from davosbot import brain, personality, request_context


class BoundaryRetentionTests(unittest.TestCase):
    def mandatory(self):
        return personality._mandatory_behavior_instructions().strip()

    def assert_boundaries(self, prompt):
        self.assertIn(self.mandatory(), prompt)
        for line in self.mandatory().splitlines():
            if line.startswith('- '):
                self.assertIn(line, prompt)

    def test_real_full_rule_layout_survives_old_cutoff(self):
        rules = personality._core_behavior_instructions()
        self.assertGreater(rules.index('### Safety and permission rules'), brain._BANTER_RULES_CHARS)
        system = 'Synthetic identity\n\n' + rules
        compact = brain._compact_banter_system(system)
        self.assert_boundaries(compact)
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assert_boundaries(brain._fit_system_for_ollama(compact))

    def test_oversized_identity_tone_and_facts_do_not_displace_boundaries(self):
        system = ('Synthetic identity ' * 2000 + '\n\n## Voice and boundaries\n' +
                  'Tone filler ' * 5000 + '\n\n## FACTS\nFACTMARKER ' * 1000)
        compact = brain._compact_banter_system(system)
        self.assert_boundaries(compact)
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assertNotIn('FACTMARKER', compact)

    def test_unsectioned_or_missing_incoming_boundaries_use_canonical_rules(self):
        for system in ('', 'Synthetic identity', 'unsectioned filler ' * 10000):
            with self.subTest(size=len(system)):
                self.assert_boundaries(brain._compact_banter_system(system))

    def test_generated_request_context_retained_and_budgeted(self):
        context = request_context._START + 'Synthetic runtime context. ' * 150 + request_context._END
        system = context + 'Synthetic identity\n\n' + personality._core_behavior_instructions()
        compact = brain._compact_banter_system(system)
        self.assertTrue(compact.startswith(context))
        self.assert_boundaries(brain._fit_system_for_ollama(compact))
        self.assertLessEqual(len(compact), brain._MAX_OLLAMA_SYSTEM_CHARS)

    def test_tone_yields_to_boundaries_in_small_budget(self):
        with patch.object(brain, '_MAX_OLLAMA_SYSTEM_CHARS', len(self.mandatory()) + 50):
            compact = brain._compact_banter_system('Synthetic identity\n\n' + personality._core_behavior_instructions())
            self.assert_boundaries(compact)
            self.assertLessEqual(len(compact), brain._MAX_OLLAMA_SYSTEM_CHARS)

    def test_insufficient_budget_never_calls_provider_without_boundaries(self):
        with patch.object(brain, '_MAX_OLLAMA_SYSTEM_CHARS', 20), patch.object(brain.requests, 'post') as post:
            result = brain._call_ollama('Synthetic identity', [], 'hello', compact_banter=True,
                                        context_fallback=brain._OLLAMA_CONTEXT_LIMIT_SENTINEL)
            self.assertEqual(brain._OLLAMA_CONTEXT_LIMIT_SENTINEL, result)
            post.assert_not_called()
