"""Render the complete installer template from the real Make export recipe."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class FinalImageTemplateTest(unittest.TestCase):
    def render(self, settings="", arguments=(), expected_cache=("", "")):
        source = (ROOT / "slave.mk").read_text()
        end = source.index('\n\tj2 -f env files/initramfs-tools/union-mount.j2')
        start = source.rindex('\n\texport debs_path="$(IMAGE_DISTRO_DEBS_PATH)"', 0, end) + 1
        # Keep the production exports, including their Make functions and
        # continuations. Omit package prerequisites and service-file generation;
        # no installer command or generated shell script is executed.
        exports = []
        lines = iter(source[start:end].splitlines(keepends=True))
        for line in lines:
            if line.startswith("\texport "):
                statement = line
                while line.rstrip().endswith("\\"):
                    line = next(lines)
                    statement += line
                exports.append(statement)
        globals_ = re.findall(
            r'^export (?:CONFIGURED_ARCH|SONIC_ROUTING_STACK|DEFAULT_CONTAINER_REGISTRY)\n',
            source, re.M)
        self.assertEqual(len(globals_), 3)
        self.assertGreater(len(exports), 60)
        with tempfile.TemporaryDirectory(prefix="final-image-template-") as directory:
            directory = Path(directory)
            renderer = directory / "render.py"
            renderer.write_text('''import json, os, pathlib, sys
from jinja2 import Environment, FileSystemLoader, StrictUndefined
root = pathlib.Path(sys.argv[1])
environment = Environment(loader=FileSystemLoader(root), undefined=StrictUndefined)
rendered = environment.get_template("files/build_templates/sonic_debian_extension.j2").render(os.environ)
pathlib.Path("rendered.sh").write_text(rendered)
pathlib.Path("flags.json").write_text(json.dumps({
    name: os.environ[name] for name in ("ENABLE_MULTIDB", "ENABLE_VRF_STRICT", "enable_multidb",
                                      "SONIC_INSTALLER_DOCKER_STORE_CACHE",
                                      "SONIC_INSTALLER_DOCKER_STORE_CACHE_MODE")
}))
''')
            (directory / "Makefile").write_text('''SHELL := /bin/bash
.SHELLFLAGS := -eu -c
.ONESHELL:
TARGET_PATH := target
IMAGE_DISTRO_DEBS_PATH := target/debs/trixie
PYTHON_DEBS_PATH := target/python-debs
PYTHON_WHEELS_PATH := target/python-wheels
FILES_PATH := target/files
CONFIGURED_PLATFORM := vs
CONFIGURED_ARCH := amd64
SONIC_ROUTING_STACK := frr
DEFAULT_CONTAINER_REGISTRY :=
KVERSION := 6.12.41+deb13-sonic-amd64
SECURE_UPGRADE_PROD_SIGNING_TOOL := fixture-signing-tool
SONIC_CONFIG_ENGINE_PY3 := sonic_config_engine.whl
sonic-vs.bin_INSTALLS := fixture.deb
sonic-vs.bin_PYTHON_WHEELS := fixture.whl
sonic-vs.bin_DOCKERS := docker-orchagent.gz docker-orchagent-dbg.gz
sonic-vs.bin_LAZY_INSTALLS := lazy.deb
lazy.deb_PLATFORM := fixture-device
sonic-vs.bin_LAZY_BUILD_INSTALLS := lazy-build.deb
lazy-build.deb_MACHINE := fixture-machine
docker-orchagent.gz_PACKAGE_NAME := swss
docker-orchagent.gz_PATH := dockers/docker-orchagent
docker-orchagent.gz_CONTAINER_NAME := swss
docker-orchagent.gz_VERSION := 1
docker-orchagent.gz_BASE_IMAGE_FILES := fixture.conf:etc/fixture.conf
SERVICES := swss.service swss@.service
''' + ''.join(globals_) + f'include {ROOT / "rules/system-health.mk"}\n' + settings +
                '\ntarget/%:\n' + ''.join(exports) +
                f'\t{sys.executable} {renderer} {ROOT}\n')
            # Do not inherit wrapper flags or ambient build settings: GNU Make
            # must provide the child environment itself, even for empty values.
            result = subprocess.run(
                ["make", "--no-print-directory", "-f", "Makefile", "target/sonic-vs.bin", *arguments],
                cwd=directory, env={"PATH": os.defpath, "LC_ALL": "C"},
                text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = (directory / "rendered.sh").read_text()
            syntax = subprocess.run(["bash", "-n", "rendered.sh"], cwd=directory,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            flags = json.loads((directory / "flags.json").read_text())
            self.assertEqual((flags.pop("SONIC_INSTALLER_DOCKER_STORE_CACHE"),
                              flags.pop("SONIC_INSTALLER_DOCKER_STORE_CACHE_MODE")), expected_cache)
            return rendered, flags

    def test_default_empty_flags_render_complete_template(self):
        rendered, flags = self.render()
        self.assertEqual(flags, {"ENABLE_MULTIDB": "", "ENABLE_VRF_STRICT": "", "enable_multidb": ""})
        self.assertNotIn('touch $FILESYSTEM_ROOT_ETC_SONIC/enable_multidb', rendered)
        self.assertNotIn('systemctl enable set-vrf-strict-mode', rendered)
        self.assertIn('install_pip_package target/python-wheels/sonic_config_engine.whl', rendered)
        self.assertIn('docker-orchagent-dbg.gz', rendered)

    def test_make_settings_and_command_line_overrides(self):
        # File assignments do not automatically export their values, unlike
        # Make command-line assignments; this detects a missing recipe export.
        for value in ("y", "n", ""):
            with self.subTest(value=value):
                rendered, flags = self.render(f'ENABLE_MULTIDB := {value}\nENABLE_VRF_STRICT := {value}\n')
                self.assertEqual(set(flags.values()), {value})
                self.assertEqual('touch $FILESYSTEM_ROOT_ETC_SONIC/enable_multidb' in rendered, value == "y")
                self.assertEqual('systemctl enable set-vrf-strict-mode' in rendered, value == "y")
        rendered, flags = self.render('ENABLE_MULTIDB := y\nENABLE_VRF_STRICT := y\n',
                                      ("ENABLE_MULTIDB=n", "ENABLE_VRF_STRICT="))
        self.assertEqual(flags, {"ENABLE_MULTIDB": "n", "ENABLE_VRF_STRICT": "", "enable_multidb": "n"})
        self.assertNotIn('systemctl enable set-vrf-strict-mode', rendered)

    def test_optional_vs_feature_branches_render(self):
        rendered, _ = self.render('''ENABLE_ZTP := y
INCLUDE_SYSTEM_EVENTD := y
INCLUDE_SYSTEM_OTEL := y
INCLUDE_BOOTCHART := y
ENABLE_BOOTCHART := y
INCLUDE_KUBERNETES := y
INCLUDE_KUBERNETES_MASTER := y
DEFAULT_CONTAINER_REGISTRY := fixture.example
ENABLE_MULTIDB := y
ENABLE_VRF_STRICT := y
''')
        self.assertIn('systemctl enable kubelet.service', rendered)
        self.assertIn('systemctl enable set-vrf-strict-mode', rendered)

    def test_cache_hit_skips_only_loading_tags_and_shutdown_generation(self):
        rendered, _ = self.render('''SONIC_INSTALLER_DOCKER_STORE_CACHE := fixture-cache
SONIC_INSTALLER_DOCKER_STORE_CACHE_MODE := read
sonic-vs.bin_DOCKERS += docker-other.gz
docker-other.gz_MACHINE := other
''', expected_cache=("fixture-cache", "read"))
        loading = "installer_store_loading_skipped=n" + rendered.split(
            "installer_store_loading_skipped=n", 1)[1].split(
                '\nif [[ -n ${SONIC_IMAGE_TIMING_FILE:-}', 1)[0]
        shutdown = rendered.split("# Generate shutdown order\n", 1)[1].split("\n\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="installer-cache-template-") as directory:
            directory = Path(directory)
            env = dict(os.environ, TARGET_MACHINE="vs", FILESYSTEM_ROOT="fixture-root",
                       DOCKER_HOST="fixture-socket", SONIC_IMAGE_VERSION="fixture-version",
                       SONIC_INSTALLER_DOCKER_STORE_OUTPUT="dockerfs.tar.gz")
            stub = '''
set -euo pipefail
sudo() {
    printf '%s\\n' "$*" >> events
    if [[ $* == *" inspect "* ]]; then echo '{}'; fi
}
jq() { if [[ $1 != -e ]]; then cat >/dev/null; echo '{}'; fi; }
python3() { echo prepare >> events; return "$PREPARE_STATUS"; }
'''
            script = stub + loading.replace("/tmp/manifest.json", str(directory / "manifest.json")) + "\n" + shutdown
            for enabled, status in ((False, "1"), (True, "1"), (True, "2"), (True, "0")):
                with self.subTest(enabled=enabled, status=status):
                    (directory / "events").write_text("")
                    current = dict(env, PREPARE_STATUS=status)
                    current.pop("SONIC_INSTALLER_DOCKER_STORE_STATE", None)
                    if enabled:
                        current["SONIC_INSTALLER_DOCKER_STORE_STATE"] = "fixture-state"
                    result = subprocess.run(["bash", "-c", script], cwd=directory, env=current,
                                            text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    events = (directory / "events").read_text().splitlines()
                    if enabled and status == "0":
                        self.assertEqual(events, ["prepare"])
                    else:
                        self.assertEqual(sum("docker load " in event for event in events), 2)
                        self.assertEqual(sum("docker tag " in event for event in events), 4)
                        self.assertEqual(sum("generate_shutdown_order.py" in event for event in events), 1)
                        self.assertFalse(any("docker-other" in event for event in events))

    def test_local_package_cache_hit_preserves_loading_installation_and_shutdown_generation(self):
        rendered, _ = self.render('''SONIC_PACKAGES_LOCAL := docker-macsec.gz docker-dhcp-relay.gz
docker-macsec.gz_DEFAULT_FEATURE_STATE_ENABLED := y
docker-dhcp-relay.gz_DEFAULT_FEATURE_STATE_ENABLED := y
''')
        loading = "installer_store_loading_skipped=n" + rendered.split(
            "installer_store_loading_skipped=n", 1)[1].split(
                '\nif [[ -n ${SONIC_IMAGE_TIMING_FILE:-}', 1)[0]
        shutdown = rendered.split("# Generate shutdown order\n", 1)[1].split("\n\n", 1)[0]
        start = rendered.index('sudo LANG=C DOCKER_HOST="$DOCKER_HOST" chroot $FILESYSTEM_ROOT '
                               '"${package_install[@]}" --from-tarball')
        packages = rendered[start:rendered.index('sudo umount $FILESYSTEM_ROOT/target', start)]
        with tempfile.TemporaryDirectory(prefix="installer-cache-local-packages-") as directory:
            directory = Path(directory)
            stub = '''
set -euo pipefail
sudo() {
    printf '%s\\n' "$*" >> events
    if [[ $* == *" inspect "* ]]; then echo '{}'; fi
    if [[ $* == *" images "* ]]; then echo latest; fi
}
jq() { if [[ $1 != -e ]]; then cat >/dev/null; echo '{}'; fi; }
python3() { echo prepare >> events; return "$PREPARE_STATUS"; }
'''
            script = stub + loading.replace("/tmp/manifest.json", str(directory / "manifest.json"))
            script += "\n" + shutdown + "\n" + packages
            for status in ("0", "1"):
                with self.subTest(status=status):
                    (directory / "events").write_text("")
                    result = subprocess.run(
                        ["bash", "-c", script], cwd=directory,
                        env=dict(os.environ, TARGET_MACHINE="vs", FILESYSTEM_ROOT="fixture-root",
                                 DOCKER_HOST="fixture-socket", SONIC_IMAGE_VERSION="fixture-version",
                                 SONIC_INSTALLER_DOCKER_STORE_STATE="fixture-state",
                                 SONIC_INSTALLER_DOCKER_STORE_OUTPUT="dockerfs.tar.gz",
                                 PREPARE_STATUS=status),
                        text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    events = (directory / "events").read_text().splitlines()
                    self.assertEqual(sum("docker load " in event for event in events), 2)
                    self.assertEqual(sum("generate_shutdown_order.py" in event for event in events), 1)
                    self.assertEqual(sum("install -y -v DEBUG --from-tarball target/docker-" in event
                                         for event in events), 2)
                    self.assertEqual(sum("docker tag docker-macsec:latest" in event for event in events), 1)
                    self.assertEqual(sum("docker tag docker-dhcp-relay:latest" in event
                                         for event in events), 1)

    def test_verified_hit_skips_packing_but_damaged_restored_output_cannot_fall_back_to_empty_store(self):
        source = (ROOT / "build_debian.sh").read_text()
        block = "installer_store_cache_status=1" + source.split(
            "installer_store_cache_status=1", 1)[1].split(
                '\nif [[ -n ${SONIC_IMAGE_TIMING_FILE:-}', 1)[0]
        with tempfile.TemporaryDirectory(prefix="installer-cache-packing-") as directory:
            directory = Path(directory)
            (directory / "root").mkdir()
            stub = '''
set -e
sudo() { echo "$*" >> "$EVENTS"; }
python3() {
    case "$2" in
        hit) return "$HIT_STATUS" ;;
        publish) echo published >> "$EVENTS" ;;
    esac
}
die() { echo "$*" >&2; exit 1; }
FILESYSTEM_ROOT=root
FILESYSTEM_DOCKERFS=dockerfs.tar.gz
BUILD_REDUCE_IMAGE_SIZE=n
'''
            for status, expected in (("0", 0), ("1", 0), ("2", 1)):
                with self.subTest(status=status):
                    (directory / "events").write_text("")
                    result = subprocess.run(["bash", "-c", stub + block], cwd=directory,
                                            env=dict(os.environ, HIT_STATUS=status,
                                                     EVENTS=str(directory / "events"),
                                                     SONIC_INSTALLER_DOCKER_STORE_STATE="fixture-state"),
                                            text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    events = (directory / "events").read_text()
                    self.assertEqual("tar -I pigz" in events, status == "1")
                    self.assertEqual("published" in events, status == "1")


if __name__ == "__main__":
    unittest.main()
