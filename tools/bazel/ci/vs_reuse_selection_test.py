"""GNU Make fixtures for the public metadata capture; no package builders run."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("vs_reuse_selection.mk")


class CaptureTests(unittest.TestCase):
    def fixture(self, extra=""):
        temporary = tempfile.TemporaryDirectory(prefix="vsm-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "target/debs/trixie").mkdir(parents=True)
        text = (
            "SHELL := /bin/false\n"
            "TARGET_PATH := target\nRULES_PATH := rules\nDEBS_PATH := target/debs/trixie\n"
            "LINUX_KERNEL_VERSION := 6.1\nCONFIGURED_ARCH := amd64\n"
            "IMAGE_DISTRO_DEBS_PATH := target/debs/trixie\n"
            "PYTHON_WHEELS_PATH := target/python-wheels/trixie\n"
            "SONIC_DEB_BUILD_POLICY := reuse-only\n"
            "SONIC_BAZEL_KERNEL_PREPARE := verify\n"
            "SONIC_BAZEL_VS_ARCHIVE_PROFILE := final23\n"
            "sonic-vs.bin_MACHINE := vs\n"
            + extra + "\ninclude " + str(SOURCE) + "\n"
            ".PHONY: metadata-fixture forbidden-producer\n"
            "metadata-fixture:\n\t$(call _vs_reuse_capture_inputs)\n"
            "forbidden-producer:\n\tfalse\n"
        )
        (root / "fixture.mk").write_text(text)
        return root

    def run_make(self, root):
        return subprocess.run(
            ["/usr/bin/make", "--no-print-directory", "-f", "fixture.mk", "metadata-fixture"],
            cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )

    def rows(self, root):
        lines = (root / "target/sonic-vs.bin.make-inputs.tsv").read_text().splitlines()
        self.assertEqual(lines[0], "sonic-vs-make-inputs-v1")
        self.assertEqual(lines[-1], "end\tsonic-vs-make-inputs-v1")
        return [line.split("\t") for line in lines[1:-1]]

    def test_records_real_host_edges_body_inputs_and_rfs_without_a_builder(self):
        root = self.fixture(
            "SONIC_DPKG_DEBS := host_1_amd64.deb leaf_1_amd64.deb\n"
            "SONIC_PYTHON_WHEELS := a.whl b.whl\n"
            "host_1_amd64.deb_DEPENDS := leaf_1_amd64.deb\n"
            "host_1_amd64.deb_WHEEL_DEPENDS := a.whl\n"
            "host_1_amd64.deb_PHONIES := forbidden-producer\n"
            "host_1_amd64.deb_CACHE_OVERRIDE := none\n"
            "a.whl_DEPENDS := b.whl\n"
            "a.whl_DEBS_DEPENDS := host_1_amd64.deb\n"
            "FIPS_BASEIMAGE_INSTALLERS := fips_1_amd64.deb\n"
            "REDIS_DUMP_LOAD_PY3 := redis.whl\n"
            "INCLUDE_SYSTEM_EVENTD := y\n"
            "BUILD_REDUCE_IMAGE_SIZE := n\n"
            "SONIC_RFS_TARGETS := sonic-vs.bin__vs__rfs.squashfs\n"
            "sonic-vs.bin_RFS_DEPENDS := sonic-vs.bin__vs__rfs.squashfs\n"
            "sonic-vs.bin__vs__rfs.squashfs_INSTALLER := sonic-vs.bin\n"
            "sonic-vs.bin__vs__rfs.squashfs_MACHINE := vs\n"
        )
        result = self.run_make(root)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        rows = self.rows(root)
        deb = next(row for row in rows if row[:2] == ["deb", "host_1_amd64.deb"])
        self.assertEqual(deb[2:4], ["leaf_1_amd64.deb", "a.whl"])
        self.assertEqual(deb[5:7], ["forbidden-producer", "none"])
        wheel = next(row for row in rows if row[:2] == ["wheel", "a.whl"])
        self.assertEqual(wheel[2:4], ["b.whl", "host_1_amd64.deb"])
        self.assertIn(["variable", "FIPS_BASEIMAGE_INSTALLERS", "fips_1_amd64.deb"], rows)
        self.assertIn(["variable", "REDIS_DUMP_LOAD_PY3", "redis.whl"], rows)
        self.assertIn(["variable", "INCLUDE_SYSTEM_EVENTD", "y"], rows)
        self.assertIn(["variable", "BUILD_REDUCE_IMAGE_SIZE", "n"], rows)
        self.assertIn(["rfs", "sonic-vs.bin__vs__rfs.squashfs", "", "sonic-vs.bin", "vs"], rows)
        self.assertFalse((root / "target/debs/trixie/host_1_amd64.deb").exists())

    def test_globs_capture_every_match_without_changing_existing_inputs(self):
        root = self.fixture()
        files = [root / ("target/debs/trixie/libyang3_" + version + "_amd64.deb") for version in ("1", "2")]
        files += [root / ("target/debs/trixie/" + name) for name in
                  ("initramfs-tools-core_1_all.deb", "initramfs-tools_1_all.deb",
                   "linux-image-6.1-sonic_1_amd64.deb", "linux-image-6.1-sonic-dbg_1_amd64.deb")]
        for path in files:
            path.write_bytes(b"literal fixture only\n")
        old = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
        self.assertEqual(self.run_make(root).returncode, 0)
        row = next(row for row in self.rows(root) if row[:2] == ["body-glob", "libyang3_*.deb"])
        self.assertEqual(row[2].split(), [str(path.relative_to(root)) for path in files[:2]])
        rows = self.rows(root)
        for pattern, selected in (("initramfs-tools-core_*.deb", files[2:3]),
                                  ("initramfs-tools_*.deb", files[3:4]),
                                  ("linux-image-6.1-*_amd64.deb", files[4:])):
            match = next(row for row in rows if row[:2] == ["body-glob", pattern])
            self.assertEqual(match[2].split(), sorted(str(path.relative_to(root)) for path in selected))
        self.assertEqual(old, [(path.read_bytes(), path.stat().st_mtime_ns) for path in files])

    def test_openssh_globs_record_unexpected_architecture_siblings(self):
        root = self.fixture("OPENSSH_VERSION_FULL := 10.0p1-7\n")
        for stem in ("openssh-server", "openssh-client", "openssh-sftp-server"):
            files = [root / ("target/debs/trixie/" + stem + "_10.0p1-7_" + arch + ".deb")
                     for arch in ("amd64", "arm64")]
            for path in files:
                path.write_bytes(b"literal fixture only\n")
        self.assertEqual(self.run_make(root).returncode, 0)
        rows = self.rows(root)
        for stem in ("openssh-server", "openssh-client", "openssh-sftp-server"):
            row = next(row for row in rows if row[:2] == ["body-glob", stem + "_10.0p1-7_*.deb"])
            self.assertEqual(row[2].split(), ["target/debs/trixie/" + stem + "_10.0p1-7_" + arch + ".deb"
                                            for arch in ("amd64", "arm64")])
        self.assertIn(["body-glob", "sonic-ztp_*.deb", ""], rows)

    def test_public_values_are_data_and_private_values_are_omitted(self):
        value = "value " + chr(96) + "touch executed" + chr(96)
        root = self.fixture(
            "SONIC_IMAGE_VERSION := " + value + "\n"
            "PASSWORD := fixture-secret-do-not-export\n"
            "SONIC_INSTALLER_EXTRA_EXPORTS := SECRET=fixture-private-export\n"
        )
        self.assertEqual(self.run_make(root).returncode, 0)
        rows = self.rows(root)
        self.assertIn(["variable", "SONIC_IMAGE_VERSION", value], rows)
        self.assertIn(["extra-exports-present", "y"], rows)
        self.assertIn(["optional-input", "rules/config.organization", "absent"], rows)
        payload = (root / "target/sonic-vs.bin.make-inputs.tsv").read_text()
        self.assertNotIn("fixture-secret", payload)
        self.assertNotIn("fixture-private", payload)
        self.assertNotIn("PASSWORD", payload)
        self.assertFalse((root / "executed").exists())

    def test_refuses_existing_capture_without_replacing_it(self):
        root = self.fixture()
        self.assertEqual(self.run_make(root).returncode, 0)
        path = root / "target/sonic-vs.bin.make-inputs.tsv"
        old = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        result = self.run_make(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"requires fresh selection outputs", result.stderr)
        self.assertEqual(old, (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns))

    def test_refuses_existing_selection_before_capture(self):
        root = self.fixture()
        existing = root / "target/sonic-vs.bin.selection.json"
        existing.write_text("previous fixture\n")
        self.assertNotEqual(self.run_make(root).returncode, 0)
        self.assertFalse((root / "target/sonic-vs.bin.make-inputs.tsv").exists())
        self.assertEqual(existing.read_text(), "previous fixture\n")

    def test_refuses_a_different_target_path(self):
        for value in ("other", "target other", "target ", "target/../target"):
            with self.subTest(value=value):
                root = self.fixture("TARGET_PATH := " + value + "\n")
                result = self.run_make(root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"requires TARGET_PATH=target", result.stderr)
                self.assertFalse((root / "target/sonic-vs.bin.make-inputs.tsv").exists())

    def configured_fixture(self, extra=""):
        root = self.fixture(extra)
        path = root / "fixture.mk"
        wrapper = SOURCE.with_name("vs_configured_inputs.mk")
        text = path.read_text().replace("include " + str(SOURCE) + "\n",
            "SONIC_VS_INPUT_INVENTORY := y\ninclude " + str(wrapper) + "\n")
        path.write_text(text)
        return root

    def run_configured(self, root, goals=("vs-input-inventory",)):
        return subprocess.run(
            ["/usr/bin/make", "--no-builtin-rules", "--no-print-directory", "-f", "fixture.mk", *goals],
            cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )

    def test_configured_goal_does_not_schedule_selection_or_package_inputs(self):
        root = self.configured_fixture(
            "SONIC_BAZEL_MANIFESTS := forbidden-manifest\n"
            "SONIC_BAZEL_IMAGE_ENVIRONMENT_FILES := forbidden-producer\n"
            "SONIC_MAKE_DEBS := absent_1_amd64.deb\n")
        result = self.run_configured(root)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(["variable", "SONIC_MAKE_DEBS", "absent_1_amd64.deb"], self.rows(root))
        self.assertFalse((root / "target/sonic-vs.bin.dockers").exists())
        self.assertFalse((root / "target/sonic-vs.bin.selection.json").exists())
        self.assertFalse((root / "target/bazel-manifests").exists())
        self.assertFalse((root / "target/debs/trixie/absent_1_amd64.deb").exists())

    def test_configured_goal_refuses_mixed_targets_before_capture(self):
        root = self.configured_fixture()
        result = self.run_configured(root, ("vs-input-inventory", "forbidden-producer"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"one metadata goal only", result.stderr)
        self.assertFalse((root / "target/sonic-vs.bin.make-inputs.tsv").exists())

    def test_configured_goal_never_remakes_absent_optional_includes(self):
        root = self.configured_fixture(
            "-include rules/config.user\n"
            "%.user: forbidden-producer\n")
        result = self.run_configured(root)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse((root / "rules/config.user").exists())
        self.rows(root)

    def test_configured_goal_rejects_explicit_optional_include_producer(self):
        root = self.configured_fixture(
            "-include rules/config.user\n"
            "rules/config.user: forbidden-producer\n")
        result = self.run_configured(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"both : and ::", result.stderr)
        self.assertFalse((root / "target/sonic-vs.bin.make-inputs.tsv").exists())



if __name__ == "__main__":
    unittest.main()
