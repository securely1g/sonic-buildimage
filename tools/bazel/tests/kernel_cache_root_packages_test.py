"""Exercise the root kernel package command across a trusted build and a fresh job."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.kernel import packages, remote_bundle, root_request, root_resolution
from tools.bazel.tests import kernel_test, kernel_cache_root_resolution_test as proof


class RootPackagesTest(unittest.TestCase):
    def setUp(self):
        fixture = kernel_test.KernelBundleTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.workspace, self.directory = fixture.root, fixture.bundle
        self.architecture = fixture.architecture
        self.lock, self.responses = proof.resolution_fixture(self.workspace)
        self.enterContext(proof.fixture_policy(self.workspace, self.lock))
        self.downloads = []
        self.blobs = {}
        self.log = self.workspace / 'bazel-calls'
        outputs = self.workspace / 'bazel-outs'
        outputs.mkdir()
        paths = []
        for name in fixture.packages:
            # The registry producer emits a directory artifact for each sonic_deb.
            parent = outputs / (name + 's')
            parent.mkdir()
            (self.directory / name).rename(parent / name)
            paths.append(str(parent))
        (self.directory / packages.MANIFEST).rename(outputs / packages.MANIFEST)
        paths.append(str(outputs / packages.MANIFEST))
        shutil.rmtree(self.directory)
        self.bazel = self.workspace / 'fixture-bazel'
        self.bazel.write_text(
            '#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
            "command = sys.argv[1]\nwith Path('bazel-calls').open('a') as log: print(command, file=log)\n"
            "if command == 'build': Path('MODULE.bazel.lock').write_bytes(Path('producer.lock').read_bytes())\n"
            'elif command == "cquery": print(' + repr('\n'.join(paths)) + ')\n'
            'else: raise ValueError(command)\n')
        self.bazel.chmod(0o755)
        (self.workspace / 'producer.lock').write_bytes(self.lock)

        def download(digest, size, dest):
            self.downloads.append(dest.name)
            dest.write_bytes(self.blobs[digest, size])

        def upload(digest, size, src):
            data = src.read_bytes()
            self.assertEqual((digest, size), (hashlib.sha256(data).hexdigest(), len(data)))
            self.blobs[digest, size] = data

        cache = Mock(download=download, upload=upload, missing=lambda items: set(items) - self.blobs.keys())
        self.enterContext(patch.object(remote_bundle.cache_transport, 'BlobCache',
                                       return_value=contextlib.nullcontext(cache)))
        self.enterContext(patch.object(root_request, 'request_key', return_value='a' * 64))
        self.enterContext(patch.dict(os.environ, {'SONIC_VS_KERNEL_REMOTE_REUSE': '1', 'BAZEL': str(self.bazel)}))
        real_validate = root_resolution.validate
        self.enterContext(patch.object(root_resolution, 'validate',
                                      side_effect=lambda data, workspace: real_validate(
                                          data, workspace, fetch=self.responses.__getitem__, environment={})))
        self.build(upload=True)
        self.assertEqual(self.log.read_text().splitlines(), ['build', 'cquery'])
        self.original = {p.name: p.read_bytes() for p in self.directory.iterdir() if not p.name.startswith('.')}
        shutil.rmtree(self.directory)
        self.log.unlink()
        (self.workspace / 'MODULE.bazel.lock').unlink()
        self.index = self.workspace / remote_bundle.INPUT_INDEX
        self.index.parent.mkdir(parents=True)
        (self.workspace / remote_bundle.OUTPUT_INDEX).rename(self.index)

    def build(self, upload=False):
        args = ['packages.py', 'build', '--workspace', str(self.workspace), '--debs-directory', str(self.directory),
                '--architecture', self.architecture, '--remote-cache', 'grpc://fixture:9092']
        if upload:
            args.append('--upload-local-results')
        with patch.object(sys, 'argv', args):
            packages.main()

    def test_fresh_job_restores_original_five_debs_and_receipt_without_invoking_bazel(self):
        self.build()
        self.assertFalse(self.log.exists())
        self.assertEqual(self.original, {p.name: p.read_bytes() for p in self.directory.iterdir()
                                         if not p.name.startswith('.')})
        self.assertEqual(5, sum(name.endswith('.deb') for name in self.downloads))
        self.assertFalse((self.workspace / remote_bundle.OUTPUT_INDEX).exists())
        provenance = json.loads(self.original[packages.PROVENANCE])
        self.assertEqual(provenance['resolution']['lock_sha256'], hashlib.sha256(self.lock).hexdigest())

    def test_transitive_change_or_lookup_failure_uses_bazel_before_fetching_debs(self):
        url = next(url for url in self.responses if '/patchelf/' in url and url.endswith('/source.json')
                   and self.responses[url] is not None)
        changed_source = json.loads(self.responses[url])
        changed_source['integrity'] = 'sha256-' + 'B' * 43 + '='
        for response in (json.dumps(changed_source).encode(), None):
            with self.subTest(response_missing=response is None):
                self.responses[url] = response
                self.build()
                self.assertEqual(self.log.read_text().splitlines(), ['build', 'cquery'])
                self.assertFalse(any(name.endswith('.deb') for name in self.downloads))
                self.log.unlink()

    def test_evicted_package_and_legacy_index_use_normal_root_build(self):
        index = json.loads(self.index.read_text())
        pkg = json.loads(self.original[packages.MANIFEST])['packages'][0]
        self.blobs.pop((pkg['sha256'], pkg['size']))
        for schema in (index['schema'], 1):
            with self.subTest(schema=schema):
                self.index.write_text(json.dumps({**index, 'schema': schema}))
                self.build()
                self.assertEqual(self.log.read_text().splitlines(), ['build', 'cquery'])
                self.assertFalse(any(name.endswith('.deb') for name in self.downloads))
                self.log.unlink()


if __name__ == '__main__':
    unittest.main()
