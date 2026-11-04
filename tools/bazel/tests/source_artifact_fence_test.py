"""Keep prior image evidence while rejecting changes to the checkout's inputs."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.bazel.ci import (dash_engine_image, dash_ha_image, gbsyncd_image, gnmi_image,
                            lldp_image, macsec_image, mux_image, orchagent_image,
                            otel_image, p4rt_image, sflow_image, syncd_image, sysmgr_image)


class SourceArtifactFenceTest(unittest.TestCase):
    driver = sysmgr_image
    method = "clean_source"
    accepts_directory = True
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.source = self.root / 'source.txt'
        self.source.write_text('original\n')
        subprocess.run(['git', 'add', 'source.txt'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture'], cwd=self.root, check=True)

    def check_source(self):
        with patch.object(self.driver, 'ROOT', self.root):
            function = getattr(self.driver, self.method)
            function(self.root / 'artifacts' / 'current') if self.accepts_directory else function()

    def test_prior_family_evidence_can_coexist(self):
        evidence = self.root / 'artifacts' / 'orchagent' / 'receipt.json'
        evidence.parent.mkdir(parents=True)
        evidence.write_text('{}\n')
        self.check_source()

    def test_untracked_source_still_rejected(self):
        (self.root / 'undeclared.py').write_text('pass\n')
        with self.assertRaisesRegex(ValueError, '(untracked source inputs|commit all source inputs|undeclared inputs)'):
            self.check_source()

    def test_tracked_source_still_rejected(self):
        self.source.write_text('changed\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.check_source()

    def test_artifacts_name_alone_is_not_reserved_subtree(self):
        (self.root / 'artifacts').write_text('undeclared input\n')
        with self.assertRaisesRegex(ValueError, '(untracked source inputs|commit all source inputs|undeclared inputs)'):
            self.check_source()


class GearboxSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = gbsyncd_image


class SyncdSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = syncd_image


class DashEngineSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = dash_engine_image
    method = "clean_source"
    accepts_directory = True


class DashHASourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = dash_ha_image
    method = "clean_source"
    accepts_directory = True


class GNMISourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = gnmi_image
    method = "source_revision"
    accepts_directory = False


class LLDPSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = lldp_image
    method = "clean_source"
    accepts_directory = True


class MACsecSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = macsec_image
    method = "clean_source"
    accepts_directory = True


class MUXSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = mux_image
    method = "checkout_revision"
    accepts_directory = False


class OrchagentSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = orchagent_image
    method = "identity"
    accepts_directory = True


class OTELSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = otel_image
    method = "clean_source"
    accepts_directory = True


class P4RTSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = p4rt_image
    method = "source_identity"
    accepts_directory = True


class SFlowSourceArtifactFenceTest(SourceArtifactFenceTest):
    driver = sflow_image
    method = "clean_source"
    accepts_directory = True


if __name__ == '__main__':
    unittest.main()
