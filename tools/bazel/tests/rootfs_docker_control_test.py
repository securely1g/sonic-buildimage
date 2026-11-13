#!/usr/bin/env python3
"""Exercise rootfs Docker control with command stubs; never start a daemon."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "files/build_templates/sonic_debian_extension.j2"


class RootfsDockerControlTest(unittest.TestCase):
    def exercise(self, mode="default", suffix="stop_rootfs_docker\n", *, start=0, stop=0,
                 cross="n", qemu="n"):
        text = TEMPLATE.read_text()
        begin = text.index("# Configure the build-only daemon.")
        end = text.index("\ninstall_pip_package()", begin)
        block = text[begin:end]
        with tempfile.TemporaryDirectory(prefix="rootfs-docker-control-") as temporary:
            work = Path(temporary)
            (work / "fsroot/etc/default").mkdir(parents=True)
            event_file = work / "events"
            event_file.touch()
            prefix = "\n".join([
                "set -e",
                ". " + shlex.quote(str(ROOT / "functions.sh")),
                "FILESYSTEM_ROOT=" + shlex.quote(str(work / "fsroot")),
                "EVENTS=" + shlex.quote(str(event_file)),
                "DOCKER_CTL_DIR=/usr/lib/docker/",
                'DOCKER_CTL_SCRIPT="$DOCKER_CTL_DIR/docker.sh"',
                "DOCKER_SCRIPTS_DIR=files/docker",
                "SONIC_ROOTFS_DOCKER_NETWORK=" + shlex.quote(mode),
                "CROSS_BUILD_ENVIRON=" + shlex.quote(cross),
                "MULTIARCH_QEMU_ENVIRON=" + shlex.quote(qemu),
                "FAIL_START=" + str(start),
                "FAIL_STOP=" + str(stop),
                'clean_sys() { printf "%s\\n" sys-cleanup >> "$EVENTS"; }',
                "trap_push clean_sys",
                r"""sudo() {
    case "$1" in
        mkdir|cp) return 0 ;;
        tee)
            [[ $# == 3 && $2 == -a && $3 == "$FILESYSTEM_ROOT/etc/default/docker" ]] || return 94
            shift
            command tee "$@"
            ;;
        chroot)
            [[ $# == 4 && $2 == "$FILESYSTEM_ROOT" && $3 == "$DOCKER_CTL_SCRIPT" ]] || return 95
            printf '%s\n' "$4" >> "$EVENTS"
            case "$4" in
                start) return "$FAIL_START" ;;
                stop) return "$FAIL_STOP" ;;
                *) return 96 ;;
            esac
            ;;
        *) return 97 ;;
    esac
}""",
            ]) + "\n"
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-c", prefix + block + "\n" + suffix],
                cwd=work, env={"PATH": "/usr/bin:/bin", "HOME": str(work), "LC_ALL": "C"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, check=False,
            )
            config = work / "fsroot/etc/default/docker"
            return (result.returncode, event_file.read_text().splitlines(),
                    config.read_text() if config.exists() else None, result.stderr.decode())

    def test_default_options_and_single_normal_stop(self):
        code, events, config, error = self.exercise()
        self.assertEqual((code, events, error), (0, ["start", "stop", "sys-cleanup"], ""))
        self.assertEqual(config, 'DOCKER_OPTS="--storage-driver=overlay2"\n')

    def test_none_disables_network_mutations(self):
        code, events, config, error = self.exercise("none")
        self.assertEqual((code, events, error), (0, ["start", "stop", "sys-cleanup"], ""))
        self.assertEqual(config, 'DOCKER_OPTS="--storage-driver=overlay2 --bridge=none '
                         '--iptables=false --ip6tables=false --ip-forward=false --ip-masq=false"\n')

    def test_unknown_mode_fails_before_daemon_or_config_write(self):
        code, events, config, error = self.exercise("invalid")
        self.assertEqual(code, 1)
        self.assertEqual(events, ["sys-cleanup"])
        self.assertIsNone(config)
        self.assertIn("Unsupported SONIC_ROOTFS_DOCKER_NETWORK", error)

    def test_body_failure_stops_daemon_before_sysfs_and_preserves_status(self):
        code, events, _, error = self.exercise("none", suffix="exit 17\n")
        self.assertEqual((code, events, error), (17, ["start", "stop", "sys-cleanup"], ""))

    def test_failed_start_attempt_also_runs_cleanup(self):
        code, events, _, error = self.exercise("none", start=71)
        self.assertEqual((code, events, error), (71, ["start", "stop", "sys-cleanup"], ""))

    def test_failed_stop_keeps_build_failed_and_still_cleans_sysfs(self):
        code, events, _, error = self.exercise("none", stop=73)
        self.assertEqual(code, 73)
        self.assertEqual(events, ["start", "stop", "stop", "sys-cleanup"])
        self.assertIn("Failed to stop the rootfs Docker daemon during cleanup", error)

    def test_cross_build_keeps_existing_external_daemon_route(self):
        code, events, _, error = self.exercise("none", suffix='[[ $DOCKER_HOST == unix:///dockerfs/var/run/docker.sock ]]\nstop_rootfs_docker\n', cross="y")
        self.assertEqual((code, events, error), (0, ["sys-cleanup"], ""))

    def test_qemu_keeps_existing_external_daemon_route(self):
        code, events, _, error = self.exercise("none", suffix='[[ $DOCKER_HOST == unix:///dockerfs/var/run/docker.sock ]]\nstop_rootfs_docker\n', qemu="y")
        self.assertEqual((code, events, error), (0, ["sys-cleanup"], ""))

    def test_installer_exports_requested_mode_to_child(self):
        declarations = [line for line in (ROOT / "slave.mk").read_text().splitlines()
                        if ": private export SONIC_ROOTFS_DOCKER_NETWORK :=" in line]
        self.assertEqual(len(declarations), 1)
        with tempfile.TemporaryDirectory(prefix="rootfs-docker-export-") as temporary:
            work = Path(temporary)
            makefile = work / "Makefile"
            makefile.write_text(
                "TARGET_PATH := target\nSONIC_INSTALLERS := sonic-vs.bin\n"
                + declarations[0] + "\n.PHONY: target/sonic-vs.bin\n"
                'target/sonic-vs.bin:\n\t@printf "%s\\n" "$$SONIC_ROOTFS_DOCKER_NETWORK"\n'
            )
            result = subprocess.run(
                ["/usr/bin/make", "-rR", "-f", str(makefile), "target/sonic-vs.bin",
                 "SONIC_ROOTFS_DOCKER_NETWORK=none"],
                cwd=work, env={"PATH": "/usr/bin:/bin", "HOME": str(work), "LC_ALL": "C"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, check=False,
            )
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"none\n", b""))


if __name__ == "__main__":
    unittest.main()
