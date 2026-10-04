#!/usr/bin/env python3
import copy
import importlib.util
import json
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('metadata', Path(__file__).with_name('metadata.py'))
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class MetadataTest(unittest.TestCase):
    def config(self):
        return {'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': {
            'com.azure.sonic.manifest': '{"service":{"name":"swss"}}',
            'com.azure.sonic.versions.swss': '1.0.0',
            'com.azure.sonic.yang-module': 'sonic-swss',
        }}, 'rootfs': {'type': 'layers', 'diff_ids': ['sha256:old']}, 'history': [{'created_by': 'old'}]}

    def test_binary_change_keeps_host_dependency_bytes(self):
        first = self.config()
        second = copy.deepcopy(first)
        second['rootfs']['diff_ids'] = ['sha256:new']
        second['history'] = [{'created_by': 'new'}]
        second['created'] = '2030-01-01T00:00:00Z'
        self.assertEqual(metadata.canonical(metadata.project(first)), metadata.canonical(metadata.project(second)))

    def test_every_label_is_a_dependency(self):
        first = self.config()
        for key in first['config']['Labels']:
            second = copy.deepcopy(first)
            second['config']['Labels'][key] += ' '
            self.assertNotEqual(metadata.project(first), metadata.project(second))

    def test_staging_archive_is_explicitly_metadata_only_and_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.tar', Path(tmp) / 'b.tar'
            projection = metadata.project(self.config())
            metadata.staging_archive(projection, 'docker-orchagent.gz', a)
            metadata.staging_archive(projection, 'docker-orchagent.gz', b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with tarfile.open(a) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))[0]
                self.assertEqual([], manifest['Layers'])
                self.assertEqual(['docker-orchagent:latest'], manifest['RepoTags'])
                config = json.load(archive.extractfile(manifest['Config']))
                self.assertEqual([], config['rootfs']['diff_ids'])
                self.assertEqual('true', config['config']['Labels'][metadata.MARKER])
            with self.assertRaisesRegex(ValueError, 'already metadata-only'):
                metadata.from_archive(a)

    def test_docker28_config_blob_without_json_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image.tar'
            config_name = 'blobs/sha256/' + 'a' * 64
            members = [(config_name, metadata.canonical(self.config())),
                       ('manifest.json', metadata.canonical([{'Config': config_name, 'Layers': []}]))]
            with tarfile.open(path, 'w') as archive:
                for name, data in members:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            self.assertEqual(metadata.project(self.config()), metadata.from_archive(path))

    def test_missing_manifest_or_wrong_platform_rejected(self):
        for config in ({'architecture': 'arm64', 'os': 'linux'}, {'architecture': 'amd64', 'os': 'linux'}):
            with self.assertRaises(ValueError):
                metadata.project(config)


if __name__ == '__main__':
    unittest.main()
