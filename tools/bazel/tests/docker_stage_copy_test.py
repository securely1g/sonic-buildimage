"""Keep Docker's mounted network files intact when copying a configured image.

Real APT installation can generate /etc/hosts through netbase's postinst.
The shared Dockerfile macro must retain Docker's runtime mounts while copying
ordinary configuration, package-manager state and source-built programs.
"""
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[3]


class DockerStageCopyTest(unittest.TestCase):
    def test_runtime_network_files_stay_while_package_and_source_files_copy(self):
        """Exercise rendered rsync, including nested files named hosts/hostname."""
        template = Environment(loader=FileSystemLoader(str(ROOT))).from_string(
            '{% from "dockers/dockerfile-macros.j2" import rsync_from_builder_stage %}'
            '{{ rsync_from_builder_stage() }}')
        command = shlex.split(template.render())
        self.assertEqual(command[:3], [
            'RUN', '--mount=type=bind,from=base,target=/changes-to-image', 'rsync'])
        self.assertEqual(command[-2:], ['/changes-to-image/', '/'])

        with tempfile.TemporaryDirectory() as temporary:
            source, destination = [Path(temporary) / name for name in ('source', 'destination')]
            source.mkdir()
            destination.mkdir()

            def write(root, name, data):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)

            mounted = {
                'etc/hosts': '127.0.0.1 localhost\n172.18.0.2 container-runtime-name\n',
                'etc/hostname': 'container-runtime-name\n',
                'etc/resolv.conf': 'nameserver 192.0.2.1\n',
            }
            for name, contents in mounted.items():
                write(destination, name, contents)
                write(source, name, 'different image-stage content for ' + name + '\n')

            payloads = {
                'etc/networks': 'default 0.0.0.0\nloopback 127.0.0.0\n',
                'var/lib/dpkg/status': 'Package: netbase\nStatus: install ok installed\nVersion: 6.5\n',
                'var/lib/dpkg/info/netbase.postinst': '#!/bin/sh\n# package configuration script\n',
                'usr/share/sonic/bazel/source-receipt.json': '{"source": "test fixture"}\n',
                'usr/bin/test-source-program': '#!/bin/sh\necho source-built-program\n',
                'usr/share/test-data/hosts': 'copy this ordinary hosts file\n',
                'etc/test-service/hostname': 'copy this ordinary hostname file\n',
            }
            for name, contents in payloads.items():
                write(source, name, contents)
                write(destination, name, 'previous contents\n')
            executable = 'usr/bin/test-source-program'
            (source / executable).chmod(0o755)

            subprocess.run([*command[2:-2], str(source) + '/', str(destination) + '/'],
                           check=True, capture_output=True, text=True)
            for name, contents in {**mounted, **payloads}.items():
                with self.subTest(path=name):
                    self.assertEqual((destination / name).read_text(), contents)
            self.assertEqual(stat.S_IMODE((destination / executable).stat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
