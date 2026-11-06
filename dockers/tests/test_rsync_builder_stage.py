#!/usr/bin/env python3
"""Exercise the shared layer-copy command inside a disposable Docker container.

Requires root, rsync, Jinja2, and Docker-managed /etc network mounts. Never run
this test directly on a host: the destination is the container's root directory.
"""

import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest

import jinja2


ROOT = Path(__file__).resolve().parents[2]
NETWORK_FILES = (Path('/etc/hosts'), Path('/etc/hostname'), Path('/etc/resolv.conf'))


class BuilderLayerCopyTest(unittest.TestCase):
    def test_preserves_network_mounts_and_copies_package_files(self):
        self.assertEqual(os.geteuid(), 0, 'Run inside a disposable Docker container as root')
        self.assertTrue(Path('/.dockerenv').exists(), 'Refuse to run directly on a host')
        mounts = {line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()}
        self.assertTrue(all(str(path) in mounts for path in NETWORK_FILES))
        original = {path: path.read_bytes() for path in NETWORK_FILES}

        macro_path = Path(os.environ.get('SONIC_DOCKER_MACROS', ROOT / 'dockers/dockerfile-macros.j2'))
        rendered = jinja2.Template(macro_path.read_text()).module.rsync_from_builder_stage()
        command = shlex.split(next(line for line in rendered.splitlines() if line.startswith('RUN ')))
        self.assertEqual(command[:3], ['RUN', '--mount=type=bind,from=base,target=/changes-to-image', 'rsync'])
        self.assertEqual(command[-2:], ['/changes-to-image/', '/'])

        with tempfile.TemporaryDirectory(prefix='sonic-builder-layer-') as target:
            with tempfile.TemporaryDirectory(prefix='sonic-builder-source-') as source:
                source_root = Path(source)
                for path in NETWORK_FILES:
                    candidate = source_root / str(path).lstrip('/')
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.write_text('builder-stage-content-must-not-replace-runtime-mount\n')
                payload = source_root / target.lstrip('/')
                payload.mkdir(parents=True)
                program = payload / 'program'
                program.write_bytes(b'#!/bin/sh\nprintf "SONiC layer payload\\n"\n')
                program.chmod(0o751)
                os.setxattr(program, 'user.sonic-layer-test', b'preserved')
                (payload / 'program-link').symlink_to('program')
                # Anchored exclusions must not discard ordinary package files
                # named hosts or hostname elsewhere in the layer.
                for name in ('hosts', 'hostname'):
                    (payload / name).write_text('ordinary-package-data\n')

                result = subprocess.run(command[2:-2] + [source + '/', '/'],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for path, content in original.items():
                    self.assertEqual(path.read_bytes(), content, str(path))
                output = Path(target)
                self.assertEqual((output / 'program').read_bytes(), program.read_bytes())
                self.assertEqual(stat.S_IMODE((output / 'program').stat().st_mode), 0o751)
                self.assertEqual(os.getxattr(output / 'program', 'user.sonic-layer-test'), b'preserved')
                self.assertEqual(os.readlink(output / 'program-link'), 'program')
                for name in ('hosts', 'hostname'):
                    self.assertEqual((output / name).read_text(), 'ordinary-package-data\n')
                self.assertNotEqual(program.stat().st_ino, (output / 'program').stat().st_ino)


if __name__ == '__main__':
    unittest.main()
