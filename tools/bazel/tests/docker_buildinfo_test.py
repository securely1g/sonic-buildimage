"""Check Make-built children can collect buildinfo from source-built base images."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import jinja2


ROOT = Path(__file__).resolve().parents[3]


class DockerBuildinfoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        # Keep the real generator and Dockerfile templates. Stub only unrelated
        # source/version preparation; this test must not pull Docker images.
        fixtures = {
            "src/sonic-build-hooks/scripts/utils.sh": "",
            "src/sonic-build-hooks/buildinfo/fixture": "buildinfo fixture\n",
            "files/apt/apt.conf.d/fixture": "apt fixture\n",
            "scripts/docker_version_control.sh": "#!/bin/sh\nexit 0\n",
            "scripts/build_mirror_config.sh": "#!/bin/sh\nexit 0\n",
            "scripts/versions_manager.py": "#!/bin/sh\nexit 0\n",
        }
        for name, content in fixtures.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o755)

    def render(self, image):
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT))
        packages = {image.replace("-", "_") + "_" + kind: ""
                    for kind in ("debs", "pydebs", "whls", "dbgs", "pkgs", "files")}
        return environment.get_template("dockers/" + image + "/Dockerfile.j2").render(
            DOCKER_USERNAME="sonic", DOCKER_USERTAG="latest", DOCKER_BASE_ARCH="amd64",
            CONFIGURED_ARCH="amd64", **packages)

    def prepare(self, image, original=None, architecture="amd64", distro="trixie"):
        dockerfile = self.workspace / "dockers" / image / "Dockerfile"
        if original is not None:
            dockerfile.parent.mkdir(parents=True, exist_ok=True)
            dockerfile.write_text(original)
        subprocess.run(
            ["bash", str(ROOT / "scripts/prepare_docker_buildinfo.sh"), image,
             str(dockerfile.relative_to(self.workspace)), architecture, "", distro],
            cwd=self.workspace, env=dict(os.environ, SKIP_BUILD_HOOK="y"),
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return dockerfile.read_text()

    @staticmethod
    def stages(dockerfile):
        stages = []
        for line in dockerfile.splitlines():
            line = line.rstrip()
            if line.startswith("FROM "):
                stages.append([])
            if stages:
                stages[-1].append(line)
        return stages

    def assert_hooks(self, image, initialized, final_stage, original=None):
        if original is None:
            original = self.render(image)
        generated = self.prepare(image, original)
        before, after = self.stages(original), self.stages(generated)
        self.assertEqual(len(after), len(before))
        install = "RUN dpkg -i /usr/local/share/buildinfo/sonic-build-hooks_1.0_all.deb"
        pre = "RUN pre_run_buildinfo " + image
        post = "RUN post_run_buildinfo " + image
        cleanup = "RUN post_run_cleanup " + image
        for index, stage in enumerate(after):
            with self.subTest(image=image, stage=index):
                if index in initialized:
                    self.assertEqual(stage.count(install), 1)
                    self.assertEqual(stage.count(pre), 1)
                    first_run = next((line for line in before[index] if line.startswith("RUN ")), None)
                    self.assertLess(stage.index(install), stage.index(pre))
                    if first_run:
                        self.assertLess(stage.index(pre), stage.index(first_run))
                else:
                    self.assertNotIn(install, stage)
                    self.assertNotIn(pre, stage)
                if index == final_stage:
                    self.assertIn(index, initialized)
                    self.assertEqual(stage.count(post), 1)
                    self.assertEqual(stage.count(cleanup), 1)
                    self.assertLess(stage.index(pre), stage.index(post))
                    self.assertLess(stage.index(post), stage.index(cleanup))
                else:
                    self.assertNotIn(post, stage)
                    self.assertNotIn(cleanup, stage)
        self.assertEqual(self.prepare(image), generated, "preparation must be idempotent")

    def test_watchdog_builders_and_runtime_stages_have_independent_hooks(self):
        for image in ("docker-bmp-watchdog", "docker-telemetry-watchdog"):
            with self.subTest(image=image):
                self.assert_hooks(image, initialized={0, 1}, final_stage=1)
        for image in ("docker-restapi-watchdog", "docker-gnmi-watchdog"):
            with self.subTest(image=image):
                self.assert_hooks(image, initialized={0, 1}, final_stage=1)

    def test_flattening_stages_copy_the_finalized_filesystem_without_new_hooks(self):
        for image in ("docker-base-trixie", "docker-config-engine-trixie"):
            with self.subTest(image=image):
                self.assert_hooks(image, initialized={0}, final_stage=0)

    def test_single_stage_image_retains_its_hooks(self):
        self.assert_hooks("docker-config-engine-stretch", initialized={0}, final_stage=0)

    def test_native_slave_dockerfile_bytes_preserve_existing_cache_keys(self):
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT),
                                         keep_trailing_newline=True)
        for distro in ("bookworm", "trixie"):
            image = "sonic-slave-" + distro
            for architecture in ("amd64", "arm64"):
                with self.subTest(distro=distro, architecture=architecture):
                    original = environment.get_template(image + "/Dockerfile.j2").render(
                        DEFAULT_CONTAINER_REGISTRY="publicmirror.azurecr.io/",
                        CONFIGURED_ARCH=architecture, CROSS_BUILD_ENVIRON="n",
                        MULTIARCH_QEMU_ENVIRON="n", INCLUDE_FIPS="n")
                    # The original generator's byte contract, including the
                    # blank lines and trailing spaces hashed by SLAVE_BASE_TAG.
                    prologue = (
                        "# Auto-Generated for buildinfo\n"
                        "ARG SONIC_VERSION_CACHE\n"
                        "ARG SONIC_VERSION_CONTROL_COMPONENTS\n"
                        "ARG ENABLE_SBOM=n\n"
                        'COPY ["buildinfo", "/usr/local/share/buildinfo"]\n'
                        f"COPY vcache/ /sonic/target/vcache/{image}\n"
                        "RUN dpkg -i /usr/local/share/buildinfo/sonic-build-hooks_1.0_all.deb\n"
                        f"ENV IMAGENAME={image}\n"
                        f"ENV DISTRO={distro}\n"
                        "ENV ENABLE_SBOM=$ENABLE_SBOM\n"
                        f"RUN pre_run_buildinfo {image}\n\n")
                    first_run = next(line for line in original.splitlines(keepends=True)
                                     if line.startswith("RUN "))
                    expected = original.replace(first_run, prologue + first_run, 1)
                    expected += (f"\nRUN post_run_buildinfo {image} \n"
                                 f"\nRUN post_run_cleanup {image} \n")
                    generated = self.prepare(image, original, architecture, distro)
                    self.assertEqual(generated, expected)
                    self.assertEqual(self.prepare(image, architecture=architecture, distro=distro),
                                     generated, "preparation must preserve bytes on repeat")

    def test_copy_only_runtime_stage_initializes_before_finalizing(self):
        for flatten in (False, True):
            with self.subTest(flatten=flatten):
                original = (
                    "ARG BASE=docker-config-engine-trixie-sonic:latest\n"
                    "FROM $BASE AS builder\nRUN true\n"
                    "FROM $BASE AS base\n"
                    "COPY --from=builder /usr/bin/true /usr/bin/true\n"
                    "ENV DEBIAN_FRONTEND=noninteractive\n")
                if flatten:
                    original += "FROM $BASE\nCOPY --from=base / /\n"
                self.assert_hooks("docker-copy-only", initialized={0, 1}, final_stage=1,
                                  original=original)


if __name__ == "__main__":
    unittest.main()
