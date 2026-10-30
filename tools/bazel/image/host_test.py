#!/usr/bin/env python3
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
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

    def test_runtime_versions_are_checked_without_starting_a_daemon(self):
        expected = {'docker_version': '28.5.2'}
        for versions, valid in ((['Docker version 28.5.2, build test', 'containerd containerd.io 1.7.28 revision'], True),
                                (['Docker version 28.5.2, build test', 'containerd containerd.io v1.7.28 b98a3aace656320842a23f4a392a33f46af97866\n'], True),
                                (['Docker version 28.5.20, build test'], False),
                                (['Docker version 28.5.2, build test', 'containerd containerd.io 2.0.0 revision'], False)):
            with self.subTest(versions=versions), mock.patch.object(host.subprocess, 'check_output', side_effect=versions) as run:
                if valid:
                    host.validate_runtime_versions('/declared/root', expected)
                else:
                    with self.assertRaisesRegex(ValueError, 'snapshot .* version'):
                        host.validate_runtime_versions('/declared/root', expected)
                self.assertTrue(all(call.args[0][-1] == '--version' for call in run.call_args_list))

    def test_containerd_version_extensions_and_wrong_output_are_rejected(self):
        for output in ('containerd containerd.io v1.7.280 revision',
                       'containerd containerd.io v1.7.28-rc.1 revision',
                       'containerd containerd.io v1.7.28+local revision',
                       'containerd containerd.io vv1.7.28 revision',
                       'unrecognized output mentioning v1.7.28'):
            with self.subTest(output=output), mock.patch.object(host.subprocess, 'check_output', side_effect=[
                    'Docker version 28.5.2, build test', output]):
                with self.assertRaisesRegex(ValueError, 'snapshot containerd version'):
                    host.validate_runtime_versions('/declared/root', {'docker_version': '28.5.2'})

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

    def test_only_opted_in_bazel_host_skips_daemon_start_and_stop(self):
        template = (Path(__file__).resolve().parents[3] / 'files/build_templates/sonic_debian_extension.j2').read_text()
        start = template[template.index('if [[ ${SONIC_BAZEL_BUILD_STAGE:-} == host'):template.index('\ninstall_pip_package()')]
        stop = template[template.index('sudo umount $FILESYSTEM_ROOT/target'):template.index('sudo bash -c "echo {')]
        environment = dict(os.environ, FILESYSTEM_ROOT='/declared/root', DOCKER_CTL_SCRIPT='/usr/lib/docker/docker.sh',
                           SONIC_BAZEL_BUILD_STAGE='', MULTIARCH_QEMU_ENVIRON='n', CROSS_BUILD_ENVIRON='n')
        # Execute the real shell branches with a recording sudo function. No
        # mounts, chroot, or daemons are available to this regression test.
        for finalize in ('n', 'y'):
            for selected in ('n', 'y'):
                with self.subTest(finalize=finalize, selected=selected):
                    result = subprocess.run(['bash', '-eu', '-c', 'sudo() { printf "%s\\n" "$*"; };\n' + start + stop],
                                            env=dict(environment, SONIC_BAZEL_HOST_FINALIZE=finalize,
                                                     USE_CONTAINER_IMAGE_STORE_AS_BACKEND=selected),
                                            check=True, text=True, capture_output=True)
                    for action in ('start', 'stop'):
                        self.assertEqual(not (finalize == selected == 'y'),
                                         '/usr/lib/docker/docker.sh ' + action in result.stdout)

    def test_docker_backend_stages_archives_without_containerd_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'target').mkdir()
            metadata = root / 'metadata.json'
            metadata.write_text(json.dumps(host.project({
                'architecture': 'amd64', 'os': 'linux',
                'config': {'Labels': {'com.azure.sonic.manifest': '{"service":{"name":"swss"}}'}},
            })))
            local = root / 'local.gz'
            local.write_bytes(b'original local package archive')
            request = {'image_store_backend': 'docker', 'metadata': {'docker-swss.gz': str(metadata)},
                       'local_images': {'docker-macsec.gz': str(local)}}
            env = {'USE_CONTAINER_IMAGE_STORE_AS_BACKEND': 'y'}
            with mock.patch.object(host.subprocess, 'check_output', return_value='Docker version 28.5.2, build test') as version:
                with mock.patch.object(host, 'stage_offline_packages') as offline, mock.patch.object(host.subprocess, 'run') as run:
                    host.prepare_backend(root, root / 'fsroot', request, env, {'docker_version': '28.5.2'})
                    offline.assert_not_called()
                    run.assert_not_called()
                self.assertEqual(version.call_args.args[0][-2:], ['dockerd', '--version'])
            self.assertEqual(env['USE_CONTAINER_IMAGE_STORE_AS_BACKEND'], 'n')
            self.assertEqual((root / 'target/docker-macsec.gz').read_bytes(), local.read_bytes())
            with tarfile.open(root / 'target/docker-swss.gz') as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                config = json.load(archive.extractfile(manifest[0]['Config']))
                self.assertEqual(config['config']['Labels']['org.sonic.bazel.host-metadata-only'], 'true')

    def test_containerd_backend_uses_offline_host_and_target_overlay(self):
        request = {'image_store_backend': 'containerd', 'runtime_overlay': '/declared/overlay.py'}
        env = {}
        with mock.patch.object(host, 'validate_runtime_versions') as versions, \
                mock.patch.object(host, 'stage_offline_packages') as offline, \
                mock.patch.object(host.subprocess, 'run') as run:
            host.prepare_backend('/declared/source', '/declared/fsroot', request, env, {'docker_version': '28.5.2'})
            versions.assert_called_once_with('/declared/fsroot', {'docker_version': '28.5.2'})
            offline.assert_called_once()
            self.assertEqual(run.call_args.args[0], [sys.executable, '/declared/overlay.py',
                             '--rootfs', '/declared/fsroot', '--architecture', 'amd64'])
        self.assertEqual(env['USE_CONTAINER_IMAGE_STORE_AS_BACKEND'], 'y')


if __name__ == '__main__':
    unittest.main()
