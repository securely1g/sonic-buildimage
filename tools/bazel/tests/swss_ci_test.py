"""Keep SWSS source policy in the caller of shared Bazel CI helpers."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "dockers/docker-orchagent/bazel"))

import ci


class SharedRustGraphTest(unittest.TestCase):
    """Detect duplicate type providers even if their crate versions match."""

    def setUp(self):
        self.labels = {
            "serde": "@@rules_rs++crate+sonic_rust_crates__serde-1.0.228//:serde",
            "serde_core": "@@rules_rs++crate+sonic_rust_crates__serde_core-1.0.228//:serde_core",
            "swss_common": "@@sonic-swss-common+//crates/swss-common:swss_common",
        }

    def test_shared_sources_are_unique_across_target_configurations(self):
        output = "\n".join([*self.labels.values(), self.labels["serde"], "@@unrelated+//:tokio"])
        self.assertEqual(ci.shared_rust_graph(output), self.labels)

    def test_duplicate_serde_provider_is_rejected(self):
        output = "\n".join([*self.labels.values(), "@@other_graph+//:serde"])
        with self.assertRaisesRegex(ValueError, "one shared Rust target for serde"):
            ci.shared_rust_graph(output)

    def test_old_swss_graph_is_rejected(self):
        output = "\n".join([*self.labels.values(), "@@rules_rs++crate+swss_crates__tokio-1.47.1//:tokio"])
        with self.assertRaisesRegex(ValueError, "legacy SWSS-owned Rust graph"):
            ci.shared_rust_graph(output)

    def test_missing_serde_core_is_rejected(self):
        del self.labels["serde_core"]
        with self.assertRaisesRegex(ValueError, "one shared Rust target for serde_core"):
            ci.shared_rust_graph("\n".join(self.labels.values()))

    def test_matching_version_in_a_different_repository_is_rejected(self):
        self.labels["serde_core"] = "@@other_crates__serde_core-1.0.228//:serde_core"
        with self.assertRaisesRegex(ValueError, "not supplied by the shared SONiC Rust graph"):
            ci.shared_rust_graph("\n".join(self.labels.values()))


class NativePlatformTest(unittest.TestCase):
    """Use the matching declared C++ toolchain for both source and build tools."""

    def test_native_platforms_select_target_and_execution_toolchains(self):
        for architecture, machine, platform_name in (
            ("amd64", "x86_64", "x86_64_trixie"),
            ("arm64", "aarch64", "aarch64_trixie"),
        ):
            with self.subTest(architecture=architecture), \
                    mock.patch.object(ci.platform, "machine", return_value=machine), \
                    mock.patch.object(ci.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "trixie"}):
                options = ci.native_options(architecture)
                label = "@sonic_build_infra//platforms:" + platform_name
                self.assertIn("--platforms=" + label, options)
                self.assertIn("--host_platform=" + label, options)

    def test_wrong_host_is_rejected_before_building(self):
        with mock.patch.object(ci.platform, "machine", return_value="x86_64"), \
                mock.patch.object(ci.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "trixie"}):
            with self.assertRaisesRegex(ValueError, "requires native ARM64"):
                ci.native_options("arm64")

    def test_wrong_debian_release_is_rejected(self):
        with mock.patch.object(ci.platform, "machine", return_value="aarch64"), \
                mock.patch.object(ci.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "bookworm"}):
            with self.assertRaisesRegex(ValueError, "Debian Trixie"):
                ci.native_options("arm64")


class PackageArchitectureTest(unittest.TestCase):
    """Retain architecture checks when validating the assembled native layers."""

    def test_runtime_debug_and_protobuf_checks_receive_the_selected_architecture(self):
        paths = {name: Path(name) for name in ci.TARGETS}
        program = "usr/bin/orchagent"
        for machine in (62, 183):
            with self.subTest(machine=machine):
                library = ci.package_contract.library_directory(machine) + "/libswsscommon.so.0.0.0"
                required = {program, library, "usr/lib/libdashapi.so",
                            "usr/lib/python3/dist-packages/dash_api/_utils.so",
                            ci.package_contract.protobuf_runtime(machine)}
                configuration = {"usr/bin/docker-init.sh": {"mode": 0o755}}
                debug_result = {"debug_pairs": [{"path": name} for name in sorted(required)]}
                with mock.patch.object(ci.artifact_validation, "payload", side_effect=[{}, {}, {}, configuration]), \
                        mock.patch.object(ci.package_contract, "swss_contract", return_value=[program]), \
                        mock.patch.object(ci.package_contract, "source_protobuf_contract") as protobuf, \
                        mock.patch.object(ci.package_contract, "shared_library_contract", return_value={library}) as libraries, \
                        mock.patch.object(ci.artifact_validation, "debug_archives", return_value=debug_result) as debug, \
                        mock.patch.object(ci.artifact_validation, "sha", return_value="source-sha"):
                    ci.verify_packages(paths, Path("source"), machine)
                    self.assertEqual(protobuf.call_args.kwargs["expected_machine"], machine)
                    self.assertEqual(libraries.call_args.args[1], machine)
                    self.assertEqual(debug.call_args.kwargs["expected_machine"], machine)


class SourceDirectoryTest(unittest.TestCase):
    """Apply SWSS source policy after the shared helper resolves a directory."""

    def test_resolved_swss_source_must_supply_its_install_declarations(self):
        """Use the SWSS package target and reject a source tree lacking install declarations."""
        with tempfile.TemporaryDirectory() as temporary:
            output_base = Path(temporary)
            source = output_base / "external/sonic-swss+"
            (source / "dist").mkdir(parents=True)
            (source / "dist/BUILD.bazel").write_text("# Source install declarations\n")

            with mock.patch.object(ci.build, "source_directory", return_value=source) as lookup:
                self.assertEqual(ci.source_directory("bazel", [], output_base, {}), source)
                self.assertEqual(lookup.call_args.args[3], "@sonic_swss//dist:swss_pkg")
                (source / "dist/BUILD.bazel").unlink()
                with self.assertRaisesRegex(ValueError, "SWSS source lacks its install declarations"):
                    ci.source_directory("bazel", [], output_base, {})


if __name__ == "__main__":
    unittest.main()
