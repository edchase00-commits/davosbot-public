"""Workflow dependency references must also resolve inside an exported snapshot."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

class PublicDependencyManifestTests(unittest.TestCase):
    def required_manifests(self):
        workflow=(ROOT/'.github/workflows/tests.yml').read_text(encoding='utf-8')
        return set(re.findall(r'-r\s+(requirements[\w.-]*\.txt)',workflow))

    def test_every_tests_workflow_manifest_is_present(self):
        manifests=self.required_manifests()
        self.assertIn('requirements-work-images.txt',manifests)
        for name in manifests:
            with self.subTest(manifest=name):
                self.assertTrue((ROOT/name).is_file(),f'Missing workflow dependency manifest: {name}')

    def test_private_exporter_includes_workflow_manifests(self):
        exporter=ROOT/'scripts/export_public_snapshot.ps1'
        if not exporter.exists():
            self.skipTest('Private exporter intentionally omitted; snapshot presence is checked separately')
        source=exporter.read_text(encoding='utf-8')
        allowlist=source.split('$allowlistEntries = @(',1)[1].split('\n)',1)[0]
        copied=set(re.findall(r'"([^"\n]+)"',allowlist))
        self.assertFalse(self.required_manifests()-copied)
