"""Exercise the existing Make Docker-start recipe in a native hosted environment.

The Docker daemon belongs to the hosted VM. Preparing a Debian execution
environment must not edit its service configuration or attempt to launch a
second daemon; unavailable native Docker must still fail the build.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]


class NativeDockerStartTest(unittest.TestCase):
    def test_native_daemon_is_checked_without_reconfiguration_and_wait_failures_propagate(self):
        """Run the production recipe for healthy, delayed and unavailable host daemons."""
        contents = (ROOT / "slave.mk").read_text()
        recipe = "docker-start :" + contents.split("\ndocker-start :", 1)[1].split("\n\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="native-vs-docker-") as temporary:
            root = Path(temporary)
            commands = root / "commands"
            commands.mkdir()
            (root / "scripts").mkdir()
            recorder = "#!" + sys.executable + "\n" + textwrap.dedent("""
                import json, os, sys
                from pathlib import Path
                name = Path(sys.argv[0]).name
                with Path(os.environ['CALLS']).open('a') as output:
                    output.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
                if name == 'docker':
                    assert sys.argv[1:] == ['info']
                    raise SystemExit(int(os.environ['DOCKER_STATUS']))
                if name == 'wait_for_docker.sh':
                    assert sys.argv[1:] == ['60']
                    raise SystemExit(int(os.environ['WAIT_STATUS']))
                raise SystemExit('Native Docker must not change service configuration')
            """)
            for name in ("docker", "sudo", "sed", "service"):
                path = commands / name
                path.write_text(recorder)
                path.chmod(0o755)
            wait = root / "scripts/wait_for_docker.sh"
            wait.write_text(recorder)
            wait.chmod(0o755)
            (root / "Makefile").write_text(
                ".ONESHELL:\nSHELL := /bin/bash\n.SHELLFLAGS := -e -c\n"
                "SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD := y\n" + recipe + "\n"
            )
            calls = root / "calls.jsonl"
            for docker_status, wait_status in ((0, 0), (1, 0), (1, 23)):
                with self.subTest(docker_status=docker_status, wait_status=wait_status):
                    calls.unlink(missing_ok=True)
                    environment = dict(
                        os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                        CALLS=str(calls), DOCKER_STATUS=str(docker_status), WAIT_STATUS=str(wait_status),
                    )
                    result = subprocess.run(
                        ["/usr/bin/make", "--no-print-directory", "docker-start"],
                        cwd=root, env=environment, text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode == 0, not (docker_status and wait_status),
                                     result.stdout + result.stderr)
                    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                    expected = [["docker", "info"]]
                    if docker_status:
                        expected.append(["wait_for_docker.sh", "60"])
                    self.assertEqual(recorded, expected)


if __name__ == "__main__":
    unittest.main()
