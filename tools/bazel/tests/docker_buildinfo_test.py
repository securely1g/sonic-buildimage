"""Exercise Docker buildinfo generation without running Docker or packaging."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class DockerBuildinfoTest(unittest.TestCase):
    def prepare(self, dockerfile, repeat=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy(ROOT / "scripts/prepare_docker_buildinfo.sh", scripts)
            # Isolate version/cache setup; the actual Dockerfile transformer runs.
            for name in ("docker_version_control.sh", "versions_manager.py"):
                stub = scripts / name
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
            hooks = root / "src/sonic-build-hooks"
            (hooks / "scripts").mkdir(parents=True)
            (hooks / "scripts/utils.sh").write_text("")
            (hooks / "buildinfo").mkdir()
            (hooks / "buildinfo/config").write_text("fixture\n")
            context = root / "docker"
            context.mkdir()
            target = context / "Dockerfile"
            target.write_text(dockerfile)
            command = ["bash", str(scripts / "prepare_docker_buildinfo.sh"),
                       "docker-watchdog", str(target), "amd64", str(target), "trixie"]
            env = dict(os.environ, SKIP_BUILD_HOOK="y", ENABLE_VERSION_CONTROL_DOCKER="n")
            subprocess.run(command, cwd=root, env=env, check=True, capture_output=True)
            rendered = target.read_text()
            if repeat:
                subprocess.run(command, cwd=root, env=env, check=True, capture_output=True)
                self.assertEqual(target.read_text(), rendered)
            return rendered

    def assert_stage_hooks(self, rendered, expected):
        stages = []
        for line in rendered.splitlines():
            if line.startswith("FROM "):
                stages.append([])
            elif stages:
                stages[-1].append(line)
        self.assertEqual(len(stages), len(expected))
        for stage, needs_hooks in zip(stages, expected):
            installs = [i for i, line in enumerate(stage)
                        if line == "RUN dpkg -i /usr/local/share/buildinfo/sonic-build-hooks_1.0_all.deb"]
            self.assertEqual(len(installs), int(needs_hooks))
            if needs_hooks:
                commands = [line for line in stage if line.startswith("RUN ")]
                self.assertTrue(commands[0].startswith("RUN dpkg -i "))
                self.assertEqual(commands[1], "RUN pre_run_buildinfo docker-watchdog")
            if "RUN post_run_buildinfo docker-watchdog" in stage:
                self.assertTrue(needs_hooks, "post hook runs in an unprepared stage")

    def test_real_watchdogs_prepare_each_independent_stage(self):
        for name, expected in (("bmp", [True, True, False]),
                               ("telemetry", [True, True, False]),
                               ("gnmi", [True, True]),
                               ("restapi", [True, True])):
            with self.subTest(watchdog=name):
                source = ROOT / f"dockers/docker-{name}-watchdog/Dockerfile.j2"
                rendered = self.prepare(source.read_text(), repeat=True)
                self.assert_stage_hooks(rendered, expected)

    def test_single_stage_prepares_once(self):
        rendered = self.prepare("FROM debian:trixie\nRUN echo first\nRUN echo second\n")
        self.assert_stage_hooks(rendered, [True])
        self.assertIn("RUN post_run_buildinfo docker-watchdog", rendered)

    def test_final_scratch_stage_does_not_install_hooks(self):
        rendered = self.prepare("FROM debian:trixie AS base\nRUN echo build\n"
                                "FROM scratch\nCOPY --from=base /usr/bin/app /app\n")
        self.assert_stage_hooks(rendered, [True, False])


if __name__ == "__main__":
    unittest.main()
