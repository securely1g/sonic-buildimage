"""Check temporary build-time APT routing without Docker or network access."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jinja2


ROOT = Path(__file__).resolve().parents[3]
SETTING = "SONIC_BUILD_APT_DISABLE_SRV"


class BuildSlaveAptTest(unittest.TestCase):
    def test_templates_disable_srv_only_when_selected_before_first_apt_operation(self):
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT))
        options = dict(CONFIGURED_ARCH="amd64", DEFAULT_CONTAINER_REGISTRY="",
                       MULTIARCH_QEMU_ENVIRON="n", CROSS_BUILD_ENVIRON="n",
                       INCLUDE_FIPS="n", SONIC_BUILD_SLAVE_CA_SHA256="")
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                template = environment.get_template("sonic-slave-" + distro + "/Dockerfile.j2")
                default = template.render(**options)
                self.assertEqual(default, template.render(**options, **{SETTING: "n"}))
                self.assertNotIn("EnableSrvRecords", default)
                self.assertNotIn("99sonic-disable-srv", default)
                enabled = template.render(**options, **{SETTING: "y"})
                directive = 'Acquire::EnableSrvRecords "false";'
                self.assertEqual(enabled.count(directive), 1)
                self.assertLess(enabled.index(directive), enabled.index("RUN apt "))
                self.assertNotEqual(hashlib.sha1(default.encode()).hexdigest(),
                                    hashlib.sha1(enabled.encode()).hexdigest())
                self.assertEqual(enabled.count("Verify-Peer=false"), default.count("Verify-Peer=false"))
                self.assertNotIn("AllowUnauthenticated", enabled)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.slave = self.workspace / "sonic-slave-bookworm"
        self.slave.mkdir()
        self.bin = self.workspace / "bin"
        self.bin.mkdir()
        # Record the actual Make-to-Jinja environment without invoking Docker or
        # the unrelated Makefile parse-time setup commands.
        renderer = self.bin / "j2"
        renderer.write_text("#!" + sys.executable + "\nimport os, sys\n"
                            "sys.stdout.write(os.environ.get(" + repr(SETTING) + ", '<missing>'))\n")
        renderer.chmod(0o755)
        source = (ROOT / "Makefile.work").read_text()
        validation = source[source.index("# Optional native builder APT routing"):
                            source.index("# Optional host trust belongs")]
        start = source.index("# Generate the slave Dockerfile")
        render = source[start:source.index("\nifeq ($(CROSS_BUILD_ENVIRON), y)", start)]
        self.makefile = self.workspace / "test.mk"
        self.makefile.write_text("SLAVE_DIR=sonic-slave-bookworm\n" + validation + render
                                + "\nall:\n\t@printf '%s\\n' '$(SONIC_OVERRIDE_BUILD_VARS)'\n")

    def make(self, value=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", SETTING}}
        environment["PATH"] = str(self.bin) + os.pathsep + environment["PATH"]
        command = ["make", "--no-print-directory", "-f", str(self.makefile)]
        if value is not None:
            command.append(SETTING + "=" + value)
        return subprocess.run(command, cwd=self.workspace, env=environment, text=True, capture_output=True)

    def test_make_defaults_to_n_and_passes_both_supported_values_to_renderer(self):
        for value, expected in ((None, "n"), ("n", "n"), ("y", "y")):
            with self.subTest(value=value):
                result = self.make(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.slave / "Dockerfile").read_text(), expected)
                self.assertIn(SETTING + "=" + expected, result.stdout.split())

    def test_invalid_values_fail_before_rendering(self):
        for value in ("", "true", "false", "Y", "y n", "n y"):
            with self.subTest(value=value):
                result = self.make(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(SETTING + " must be y or n", result.stderr)
                self.assertFalse((self.slave / "Dockerfile").exists())

    def test_rootfs_configuration_is_opt_in_and_removed_after_package_operations(self):
        source = (ROOT / "build_debian.sh").read_text()
        start = source.index("# Build-time APT routing must not become installed SONiC configuration.")
        helpers = source[start:source.index("## Hostname for the linux image", start)]
        rootfs = self.workspace / "rootfs with spaces"
        config = rootfs / "etc/apt/apt.conf.d/99sonic-build-disable-srv"
        config.parent.mkdir(parents=True)
        unrelated = config.with_name("local-policy")
        unrelated.write_text("preserve unrelated configuration\n")
        builder_ca = self.workspace / "builder-ca.crt"
        builder_ca.write_text("fixture builder CA bundle\n")
        helpers = helpers.replace("local builder_ca=/etc/ssl/certs/ca-certificates.crt",
                                  "local builder_ca=" + repr(str(builder_ca)))
        for value in (None, "n", "y"):
            with self.subTest(value=value):
                environment = {key: value for key, value in os.environ.items() if key != SETTING}
                environment["FILESYSTEM_ROOT"] = str(rootfs)
                if value is not None:
                    environment[SETTING] = value
                script = "set -eu\nsudo() { \"$@\"; }\n" + helpers + """
sonic_configure_build_apt
if [[ -f "$FILESYSTEM_ROOT/etc/apt/apt.conf.d/99sonic-build-disable-srv" ]]; then
    cat "$FILESYSTEM_ROOT/etc/apt/apt.conf.d/99sonic-build-disable-srv"
fi
sonic_cleanup_build_apt
"""
                result = subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'Acquire::EnableSrvRecords "false";\n' if value == "y" else "")
                self.assertFalse(config.exists())
                self.assertFalse(config.with_name("99sonic-build-bootstrap-ca").exists())
                self.assertFalse(rootfs.joinpath("etc/apt/sonic-build-bootstrap-ca.crt").exists())
                self.assertEqual(unrelated.read_text(), "preserve unrelated configuration\n")

    def rootfs_helpers(self, builder_ca):
        source = (ROOT / "build_debian.sh").read_text()
        start = source.index("# Build-time APT routing must not become installed SONiC configuration.")
        helpers = source[start:source.index("## Hostname for the linux image", start)]
        # Substitute only the builder's absolute system path; execute the real
        # helper functions against an isolated rootfs and fixture trust bundle.
        return helpers.replace("local builder_ca=/etc/ssl/certs/ca-certificates.crt",
                               "local builder_ca=" + repr(str(builder_ca)))

    def run_rootfs_helpers(self, rootfs, builder_ca, commands, *, sudo='"$@"', setting="n"):
        environment = dict(os.environ, FILESYSTEM_ROOT=str(rootfs), **{SETTING: setting})
        script = ("set -eu\nsudo() { " + sudo + "; }\n"
                  "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
                  + self.rootfs_helpers(builder_ca) + commands)
        return subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True)

    def test_minbase_uses_temporary_trust_until_versioned_ca_installation(self):
        builder_ca = self.workspace / "builder bundle.crt"
        builder_ca.write_bytes(b"fixture builder CA bundle\n")
        for initial_ca in (None, b""):
            with self.subTest(initial_ca=initial_ca), tempfile.TemporaryDirectory(dir=self.workspace) as directory:
                rootfs = Path(directory)
                config = rootfs / "etc/apt/apt.conf.d/99sonic-build-bootstrap-ca"
                temporary_ca = rootfs / "etc/apt/sonic-build-bootstrap-ca.crt"
                installed_ca = rootfs / "etc/ssl/certs/ca-certificates.crt"
                srv = config.with_name("99sonic-build-disable-srv")
                config.parent.mkdir(parents=True)
                installed_ca.parent.mkdir(parents=True)
                if initial_ca is not None:
                    installed_ca.write_bytes(initial_ca)

                result = self.run_rootfs_helpers(rootfs, builder_ca, "sonic_configure_build_apt\n", setting="y")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(temporary_ca.read_bytes(), builder_ca.read_bytes())
                self.assertEqual(temporary_ca.stat().st_mode & 0o777, 0o644)
                self.assertEqual(config.read_text(),
                                 'Acquire::https::CaInfo "/etc/apt/sonic-build-bootstrap-ca.crt";\n')
                self.assertEqual(installed_ca.read_bytes() if installed_ca.exists() else None, initial_ca)

                # The unchanged version-controlled package installation creates
                # final trust. Its immediate cleanup must leave SRV routing on.
                installed_ca.write_bytes(b"version-controlled CA package bundle\n")
                result = self.run_rootfs_helpers(rootfs, builder_ca, "sonic_cleanup_build_apt_ca\n", setting="y")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(config.exists())
                self.assertFalse(temporary_ca.exists())
                self.assertTrue(srv.exists())
                self.assertEqual(installed_ca.read_bytes(), b"version-controlled CA package bundle\n")

                result = self.run_rootfs_helpers(rootfs, builder_ca, "sonic_cleanup_build_apt\n", setting="y")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(srv.exists())

    def test_existing_rootfs_trust_and_empty_cleanup_need_no_sudo(self):
        rootfs = self.workspace / "trusted rootfs"
        installed_ca = rootfs / "etc/ssl/certs/ca-certificates.crt"
        installed_ca.parent.mkdir(parents=True)
        installed_ca.write_bytes(b"existing rootfs package trust\n")
        missing_builder = self.workspace / "missing-builder-ca.crt"
        for setting, commands in (("n", "sonic_configure_build_apt\nsonic_cleanup_build_apt\n"),
                                  ("y", "sonic_cleanup_build_apt\n")):
            with self.subTest(setting=setting):
                result = self.run_rootfs_helpers(rootfs, missing_builder, commands,
                                                sudo="echo unexpected-sudo >&2; return 99", setting=setting)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("unexpected-sudo", result.stderr)
                self.assertEqual(installed_ca.read_bytes(), b"existing rootfs package trust\n")

    def test_missing_builder_trust_stops_before_configuring_https(self):
        rootfs = self.workspace / "untrusted rootfs"
        result = self.run_rootfs_helpers(rootfs, self.workspace / "absent-ca.crt",
                                        "sonic_configure_build_apt\n",
                                        sudo="echo unexpected-sudo >&2; return 99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Builder CA bundle is unavailable", result.stderr)
        self.assertNotIn("unexpected-sudo", result.stderr)
        self.assertFalse(rootfs.exists())

    def test_rootfs_setup_and_cleanup_cover_both_stages_and_every_capture_boundary(self):
        source = (ROOT / "build_debian.sh").read_text()
        setup = "sonic_configure_build_apt\n"
        cleanup = "sonic_cleanup_build_apt\n"
        self.assertLess(source.index("scripts/build_debian_base_system.sh"), source.index(setup))
        self.assertLess(source.index(setup), source.index("scripts/prepare_debian_image_buildinfo.sh"))
        self.assertLess(source.index(setup), source.index("chroot $FILESYSTEM_ROOT apt-get -y update"))
        install = source.index("apt-get -y install apt-transport-https")
        installed = source.index("sonic_cleanup_build_apt_ca\n", install)
        self.assertIn("ca-certificates", source[install:installed])
        self.assertLess(installed, source.index("curl -o /tmp/docker.asc", install))
        cleanup_function = source[source.index("sonic_cleanup_build_apt()"):
                                  source.index("## Hostname for the linux image")]
        self.assertIn("sonic_cleanup_build_apt_ca\n", cleanup_function)
        first = source.index("if [[ $RFS_SPLIT_FIRST_STAGE == y ]]; then")
        last = source.index("if [[ $RFS_SPLIT_LAST_STAGE == y ]]; then", first)
        first_stage = source[first:last]
        self.assertLess(first_stage.index(cleanup), first_stage.index("sudo mksquashfs"))
        last_stage = source[last:source.index("## Version file part 2", last)]
        self.assertLess(last_stage.index("sudo unsquashfs"), last_stage.index(setup))
        self.assertLess(source.index(setup, last), source.index("./sonic_debian_extension.sh", last))
        self.assertIn("    " + cleanup + "    sonic_bazel_write_host_snapshot\n", source)
        final_cleanup = source.index("## Clean up proxy")
        self.assertLess(source.index(cleanup, final_cleanup), source.index("sudo mksquashfs", final_cleanup))


if __name__ == "__main__":
    unittest.main()
