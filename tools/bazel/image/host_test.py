#!/usr/bin/env python3
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('host', Path(__file__).with_name('host.py'))
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostBoundaryTest(unittest.TestCase):
    def identity(self):
        return {'arch': 'amd64', 'platform': 'vs', 'machine': 'vs', 'image_type': 'onie', 'distro': 'trixie', 'image_version': 'test', 'source_commit': 'abc123', 'source_branch': 'test', 'source_date_epoch': '1'}

    def test_snapshot_from_another_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            identity = self.identity()
            state = {'format_version': 1, 'boundary': 'before-container-loading', 'identity': identity, 'state': {key: '' for key in host.STATE_FIELDS}}
            path.write_text(json.dumps(state))
            self.assertEqual(state['state'], host.snapshot_state(path, identity))
            with self.assertRaisesRegex(ValueError, 'identity'):
                host.snapshot_state(path, dict(identity, image_version='different'))

    def test_worker_requires_the_pinned_namespace_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / 'request.json'
            request.write_text(json.dumps({'init': '/declared/docker-init'}))
            for parent, same_binary in ((42, True), (1, False)):
                with mock.patch.object(host.os, 'getppid', return_value=parent), mock.patch.object(host.os.path, 'samefile', return_value=same_binary):
                    with self.assertRaisesRegex(ValueError, 'pinned init'):
                        host.worker(request)

    def test_declared_images_follow_machine_selection_and_local_packages(self):
        env = {'installer_images': 'swss|dockers/swss||target/docker-orchagent.gz:test other|dockers/other|other|target/docker-other.gz:test', 'sonic_local_packages': 'macsec|target/docker-macsec.gz|local|y'}
        self.assertEqual(({'docker-orchagent.gz'}, {'docker-macsec.gz'}), host.image_names(env))
        env['sonic_local_packages'] = 'swss|target/docker-orchagent.gz|local|y'
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            host.image_names(env)

    def test_unsafe_source_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, link in [('../outside', None), ('inside', '../../outside'), ('/outside', None)]:
                path = Path(tmp) / 'bad.tar'
                with tarfile.open(path, 'w') as archive:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                    archive.addfile(member, io.BytesIO(b''))
                with self.assertRaisesRegex(ValueError, 'unsafe|escapes'):
                    host.validate_archive(path)

    def test_network_packages_and_credentials_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for env in ({'sonic_packages': 'remote|repo|latest|local|y'}, {'PASSWORD': 'example-secret'}, {'include_kubernetes': 'y'}):
                env['SONIC_IMAGE_VERSION'] = 'test'
                path.write_text(json.dumps({'schema': 1, 'identity': self.identity(), 'environment': env}))
                with self.assertRaises(ValueError):
                    host.load_config(path)
            path.write_text(json.dumps({'schema': 1, 'identity': self.identity(), 'environment': {'CHANGE_DEFAULT_PASSWORD': 'n', 'SONIC_IMAGE_VERSION': 'test'}}))
            host.load_config(path)

    def test_host_environment_cannot_override_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            env = {'SONIC_IMAGE_VERSION': 'test', 'SOURCE_DATE_EPOCH': '1',
                   'SONIC_BAZEL_SOURCE_COMMIT': 'abc123',
                   'SONIC_BAZEL_SOURCE_BRANCH': 'test', 'CONFIGURED_ARCH': 'amd64',
                   'CONFIGURED_PLATFORM': 'vs', 'TARGET_MACHINE': 'vs',
                   'IMAGE_TYPE': 'onie', 'IMAGE_DISTRO': 'trixie'}
            config = {'schema': 1, 'identity': self.identity(), 'environment': env}
            path.write_text(json.dumps(config))
            host.load_config(path)
            for variable in env:
                with self.subTest(variable=variable):
                    path.write_text(json.dumps(dict(config, environment=dict(env, **{variable: 'different'}))))
                    with self.assertRaisesRegex(ValueError, variable):
                        host.load_config(path)
            path.write_text(json.dumps(dict(config, environment={})))
            with self.assertRaisesRegex(ValueError, 'SONIC_IMAGE_VERSION'):
                host.load_config(path)

    def test_source_selection_must_match_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.arch').write_text('amd64\n')
            (root / '.platform').write_text('vs\n')
            host.validate_source_identity(root, self.identity())
            for name, value in (('.arch', 'arm64'), ('.platform', 'other')):
                with self.subTest(name=name):
                    path = root / name
                    previous = path.read_text()
                    path.write_text(value)
                    with self.assertRaisesRegex(ValueError, 'source'):
                        host.validate_source_identity(root, self.identity())
                    path.write_text(previous)
            (root / '.arch').unlink()
            with self.assertRaisesRegex(ValueError, 'source'):
                host.validate_source_identity(root, self.identity())

    def test_custom_organization_extensions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / 'files/build_templates'
            templates.mkdir(parents=True)
            host.validate_organization_hooks(root)
            extension = templates / 'organization_extensions.sh'
            extension.write_text('echo custom')
            with self.assertRaisesRegex(ValueError, 'custom organization'):
                host.validate_organization_hooks(root)
            with mock.patch.object(host, 'digest', return_value=host.NOOP_ORGANIZATION_EXTENSION_SHA256):
                host.validate_organization_hooks(root)
            extension.unlink()
            (templates / 'build_debian.organization.sh').write_text('echo custom')
            with self.assertRaisesRegex(ValueError, 'organization build'):
                host.validate_organization_hooks(root)


if __name__ == '__main__':
    unittest.main()
