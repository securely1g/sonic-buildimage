"""Exercise P4 Make routing without invoking a compiler or producing DEBs."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[4]
PACKAGES = (
    "p4lang-pi_0.1.3-2_amd64.deb",
    "p4lang-bmv2_1.15.0-9_amd64.deb",
    "p4lang-p4c_1.2.4.2-3_amd64.deb",
    "libsai_1.0.0_amd64.deb",
    "libsai-dev_1.0.0_amd64.deb",
)


class MakeImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p4-make-routing-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for relative in (
            "rules/functions",
            "rules/p4lang.mk",
            "rules/dash-sai.mk",
            "tools/bazel/p4/inputs.mk",
            "tools/bazel/p4/debs.mk",
        ):
            destination = self.directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        (self.directory / ".platform").write_text("vs\n")
        (self.directory / "tools/bazel/p4/packages.lock.json").write_text("first\n")
        # These are text sentinels, not Debian packages. The real importer and
        # package integrity are tested separately with the retained P4 packages.
        (self.directory / "tools/bazel/p4/stage.py").write_text(textwrap.dedent(f"""\
            import argparse
            from pathlib import Path
            parser = argparse.ArgumentParser()
            parser.add_argument('--output-directory', type=Path, required=True)
            parser.add_argument('--bazel')
            parser.add_argument('--cache-directory')
            parser.add_argument('--dash-sai-commit')
            parser.add_argument('--expected-package', action='append', default=[])
            args = parser.parse_args()
            if args.dash_sai_commit != 'd5c003dd7774c2b43f275c0233acc73a0ea28d2f':
                raise SystemExit('DASH source revision differs from reviewed lock')
            if args.expected_package != list({PACKAGES!r}):
                raise SystemExit('Make package versions differ from reviewed lock')
            with Path('stage.calls').open('a') as output:
                output.write('import\\n')
            if Path('reject-import').exists():
                raise SystemExit('input verification failed')
            args.output_directory.mkdir(parents=True, exist_ok=True)
            marker = Path('tools/bazel/p4/packages.lock.json').read_text()
            for name in {PACKAGES!r}:
                output = args.output_directory / name
                output.with_name(name + '.bazel-imported').write_text('imported\\n')
                if not output.exists() or output.read_text() != marker:
                    output.write_text(marker)
            """))
        (self.directory / "Makefile").write_text(textwrap.dedent("""\
            SHELL := /bin/bash
            .ONESHELL:
            .SHELLFLAGS := -ec
            .SECONDEXPANSION:
            BUILD_WITH_BAZEL_WHEN_AVAILABLE := y
            CONFIGURED_PLATFORM := vs
            CONFIGURED_ARCH := amd64
            BLDENV := trixie
            INCLUDE_VS_DASH_SAI := y
            SRC_PATH := src
            DEBS_PATH := target/debs/$(BLDENV)
            PROTOBUF := protobuf.deb
            SCAPY := scapy.whl
            SONIC_MAKE_DEBS := unrelated.deb
            include rules/functions
            include rules/p4lang.mk
            include rules/dash-sai.mk
            include tools/bazel/p4/inputs.mk
            sbom_emit_fragment = :
            include tools/bazel/p4/debs.mk
            $(addprefix $(DEBS_PATH)/,$(SONIC_MAKE_DEBS)): $(DEBS_PATH)/%: $$(addsuffix -install,$$($$*_DEPENDS))
            \t@echo "native producer must not run: $@" >&2; exit 93
            $(addprefix $(DEBS_PATH)/,$(SONIC_DERIVED_DEBS)): $(DEBS_PATH)/%: $$(addprefix $(DEBS_PATH)/,$$($$*_DEPENDS))
            \t@echo "derived placeholder must not run: $@" >&2; exit 94
            $(addsuffix -clean,$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_CANDIDATES))):: $(DEBS_PATH)/%-clean:
            \trm -f $(addprefix $(DEBS_PATH)/,$* $($*_DERIVED_DEBS) $($*_EXTRA_DEBS))
            consumer: $(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_DEBS))
            \t@echo assembled >> consumer.calls
            \t@touch $@
            .PHONY: metadata
            metadata:
            \t@printf '%s\\n' '$(SONIC_MAKE_DEBS)' '$(SONIC_DERIVED_DEBS)' '$(SONIC_BAZEL_IMPORTED_DEBS)' '$(p4lang-p4c_1.2.4.2-3_amd64.deb_WHEEL_DEPENDS)' '$(libsai_1.0.0_amd64.deb_DEPENDS)' '$(libsai_1.0.0_amd64.deb_MOD_HASH_FILE)' > metadata.txt
            """))

    def make(self, *arguments, success=True):
        result = subprocess.run(
            ["make", "--no-print-directory", "-j8", *arguments],
            cwd=self.directory,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def metadata(self, *arguments):
        self.make("metadata", *arguments)
        return (self.directory / "metadata.txt").read_text().splitlines()

    def test_supported_profile_removes_all_native_producers(self):
        native, derived, imported, wheel, dependencies, identity = self.metadata()
        self.assertEqual(native, "unrelated.deb")
        self.assertEqual(derived, "")
        self.assertEqual(imported.split(), list(PACKAGES))
        # Installation and runtime relationships remain available to consumers.
        self.assertEqual(wheel, "scapy.whl")
        self.assertEqual(dependencies.split(), list(PACKAGES[:3]))
        self.assertEqual(identity, "tools/bazel/p4/packages.lock.json")

    def test_unsupported_profiles_keep_native_rules(self):
        for option in (
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
            "CONFIGURED_PLATFORM=broadcom",
            "CONFIGURED_ARCH=arm64",
            "BLDENV=bookworm",
            "INCLUDE_VS_DASH_SAI=n",
            "SONIC_DEBUGGING_ON=y",
            "SONIC_PROFILING_ON=y",
            "ENABLE_ASAN=y",
            "CROSS_BUILD_ENVIRON=y",
            "MULTIARCH_QEMU_ENVIRON=y",
            "ENABLE_SOURCE_ARCHIVE=y",
            "DEB_BUILD_OPTIONS=nostrip",
            "DEB_BUILD_OPTIONS_GENERIC=noopt",
        ):
            with self.subTest(option=option):
                native, derived, imported, *_ = self.metadata(option)
                self.assertEqual(imported, "")
                self.assertIn("p4lang-pi_", native)
                self.assertIn("p4lang-bmv2_", native)
                self.assertIn("p4lang-p4c_", native)
                self.assertIn("libsai_", native)
                self.assertIn("libsai-dev_", derived)

    def test_parallel_goals_import_once_without_native_prerequisites(self):
        self.make(*(f"target/debs/trixie/{name}" for name in PACKAGES))
        self.assertEqual((self.directory / "stage.calls").read_text(), "import\n")
        for name in PACKAGES:
            self.assertEqual(
                (self.directory / "target/debs/trixie" / name).read_text(),
                "first\n",
            )

    def test_missing_derived_file_is_restored(self):
        self.make("consumer")
        derived = self.directory / "target/debs/trixie" / PACKAGES[-1]
        derived.unlink()
        self.make(f"target/debs/trixie/{PACKAGES[-1]}")
        self.assertEqual(derived.read_text(), "first\n")

    def test_unchanged_import_preserves_consumer_and_changed_pin_rebuilds(self):
        self.make("consumer")
        self.make("consumer")
        self.assertEqual((self.directory / "stage.calls").read_text(), "import\n" * 2)
        self.assertEqual((self.directory / "consumer.calls").read_text(), "assembled\n")
        (self.directory / "tools/bazel/p4/packages.lock.json").write_text("updated\n")
        self.make("consumer")
        self.assertEqual((self.directory / "consumer.calls").read_text(), "assembled\n" * 2)

    def test_failed_verification_cannot_use_preexisting_files(self):
        self.make("consumer")
        (self.directory / "reject-import").touch()
        result = self.make("consumer", success=False)
        self.assertIn("input verification failed", result.stdout)
        self.assertEqual((self.directory / "consumer.calls").read_text(), "assembled\n")

    def test_make_overrides_reach_the_importer_before_staging(self):
        self.make("consumer")
        for option, error in (
            ("DASH_SAI_COMMIT=", "DASH source revision differs"),
            ("DASH_SAI_COMMIT=" + "0" * 40, "DASH source revision differs"),
            ("DASH_SAI_VERSION=2.0.0", "Make package versions differ"),
            ("P4LANG_PI_VERSION_FULL=9.9.9-1", "Make package versions differ"),
        ):
            with self.subTest(option=option):
                result = self.make("consumer", option, success=False)
                self.assertIn(error, result.stdout)
        self.assertEqual((self.directory / "consumer.calls").read_text(), "assembled\n")
        self.assertEqual((self.directory / "stage.calls").read_text(), "import\n")

    def test_switch_to_native_requires_scoped_clean(self):
        self.make("consumer")
        result = self.make(
            f"target/debs/trixie/{PACKAGES[0]}",
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
            "PROTOBUF=",
            success=False,
        )
        self.assertIn("was imported by Bazel; clean it", result.stdout)
        for name in PACKAGES:
            with self.subTest(package=name):
                result = self.make(
                    f"target/debs/trixie/{name}.bazel-native-check",
                    "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
                    success=False,
                )
                self.assertIn("was imported by Bazel; clean it", result.stdout)
                self.assertTrue((self.directory / "target/debs/trixie" / name).exists())
        for name in PACKAGES[:4]:
            self.make(
                f"target/debs/trixie/{name}-clean",
                "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
            )
        for name in PACKAGES:
            self.make(
                f"target/debs/trixie/{name}.bazel-native-check",
                "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n",
            )
            self.assertFalse((self.directory / "target/debs/trixie" / name).exists())
            self.assertFalse(
                (self.directory / "target/debs/trixie" / (name + ".bazel-imported")).exists()
            )


if __name__ == "__main__":
    unittest.main()
