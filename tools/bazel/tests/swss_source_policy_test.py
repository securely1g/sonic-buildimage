"""Protect native SWSS library inventory and shared Rust provider checks."""

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from tools.bazel.oci import package_contract
from tools.bazel.tests.integration import oci_source_layers_test as layers

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "shared_rust", ROOT / "dockers/docker-orchagent/bazel/shared_rust_graph_test.py")
graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph)


class RequiredLibrariesTest(unittest.TestCase):
    def fixture(self, architecture):
        multiarch, machine = {"amd64": ("x86_64-linux-gnu", 62),
                              "arm64": ("aarch64-linux-gnu", 183)}[architecture]
        files, libraries = {}, {}
        for name in ("libswsscommon", "libsonicdbcli", "libsairedis", "libsaimeta", "libsaimetadata"):
            prefix = "usr/lib/" + multiarch + "/" + name
            path = prefix + ".so.0.0.0"
            files[path] = {"kind": "file", "elf_machine": machine}
            files[prefix + ".so.0"] = {"kind": "symlink", "linkname": name + ".so.0.0.0"}
            libraries[path] = {prefix + ".so.0": name + ".so.0.0.0"}
        return files, {"architecture": architecture, "required_libraries": libraries}

    def test_missing_library_wrong_machine_or_soname_fails_on_both_architectures(self):
        for architecture in ("amd64", "arm64"):
            files, policy = self.fixture(architecture)
            self.assertEqual(package_contract.validate_required_libraries(files, policy),
                             set(policy["required_libraries"]))
            for path, links in policy["required_libraries"].items():
                for mutation in ("missing", "machine", "link"):
                    with self.subTest(architecture=architecture, path=path, mutation=mutation):
                        broken = copy.deepcopy(files)
                        if mutation == "missing":
                            del broken[path]
                        elif mutation == "machine":
                            broken[path]["elf_machine"] = 0
                        else:
                            broken[next(iter(links))]["linkname"] = "missing.so"
                        with self.assertRaises(ValueError):
                            package_contract.validate_required_libraries(broken, policy)

    def test_source_adapter_checks_each_library_soname_and_matching_symbols(self):
        for architecture in ("amd64", "arm64"):
            files, policy = self.fixture(architecture)
            policy.update(libraries={}, required_debug_paths=[], config_paths={},
                          install_inventory={"declaration_file": "BUILD.bazel", "install_file": "install"})
            paths = set(policy["required_libraries"])
            for missing in (None, *sorted(paths)):
                with self.subTest(architecture=architecture, missing_debug=missing), \
                     mock.patch.object(layers, "archive_hashes", return_value={}), \
                     mock.patch.object(package_contract, "validate_install_inventory", return_value=[]), \
                     mock.patch.object(layers.artifact_validation, "payload", side_effect=[{}, files, {}]), \
                     mock.patch.object(layers.artifact_validation, "sha", return_value="source-hash"), \
                     mock.patch.object(layers.artifact_validation, "debug_archives", return_value={
                         "debug_pairs": [{"path": path} for path in sorted(paths) if path != missing],
                     }) as debug:
                    arguments = [Path(name) for name in ("runtime.tar", "rdeps.tar", "config.tar", "debug.tar")]
                    if missing is None:
                        layers.verify_layers(*arguments, {}, Path("source"), policy,
                                             readelf="declared-readelf", objcopy="declared-objcopy")
                    else:
                        with self.assertRaisesRegex(ValueError, "incomplete declared native debug coverage"):
                            layers.verify_layers(*arguments, {}, Path("source"), policy,
                                                 readelf="declared-readelf", objcopy="declared-objcopy")
                    self.assertEqual(debug.call_args.kwargs["required_sonames"], {
                        path: Path(next(iter(links))).name
                        for path, links in policy["required_libraries"].items()
                    })


class SharedRustGraphTest(unittest.TestCase):
    labels = ["@@sonic-build-infra++crate+sonic_rust_crates__serde-1.0.0//:serde",
              "@@sonic-build-infra++crate+sonic_rust_crates__serde_core-1.0.0//:serde_core",
              "@@sonic-swss-common+//crates/swss-common:swss_common"]

    def test_one_provider_and_hub_required(self):
        self.assertEqual(set(graph.verify("\n".join("rust_library\t" + label for label in self.labels))), {"serde", "serde_core", "swss_common"})
        invalid = [self.labels[:-1], self.labels + [self.labels[0].replace("1.0.0", "2.0.0")],
                   [self.labels[0], self.labels[1].replace("crate+", "other+"), self.labels[2]],
                   self.labels + ["@@swss_crates__serde-1.0.0//:serde"]]
        for labels in invalid:
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                graph.verify("\n".join("rust_library\t" + label for label in labels))


if __name__ == "__main__":
    unittest.main()
