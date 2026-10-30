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
                self.assertEqual(unrelated.read_text(), "preserve unrelated configuration\n")

    def test_rootfs_setup_and_cleanup_cover_both_stages_and_every_capture_boundary(self):
        source = (ROOT / "build_debian.sh").read_text()
        setup = "sonic_configure_build_apt\n"
        cleanup = "sonic_cleanup_build_apt\n"
        self.assertLess(source.index("scripts/build_debian_base_system.sh"), source.index(setup))
        self.assertLess(source.index(setup), source.index("scripts/prepare_debian_image_buildinfo.sh"))
        self.assertLess(source.index(setup), source.index("chroot $FILESYSTEM_ROOT apt-get -y update"))
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
