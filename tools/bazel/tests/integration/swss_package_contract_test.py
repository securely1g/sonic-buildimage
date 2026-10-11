"""Check real SWSS layers against their source install and debug contracts.

The inputs are actual Bazel-built runtime, dependency, configuration and symbol
archives. Only declared source runfiles are copied into the temporary contract
tree: the test never reads Bazel's external checkout outside its sandbox.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import package_contract
from tools.bazel.ci import artifact_validation


def verify_packages(paths, source, *, readelf, objcopy):
    """Preserve the source-layer checks previously run after CI artifact copying."""
    runtime = artifact_validation.payload(paths["swss.tar"])
    programs = package_contract.swss_contract(source, runtime)
    dependencies = artifact_validation.payload(paths["rdeps.tar"])
    for name, metadata in runtime.items():
        if metadata["kind"] != "directory":
            artifact_validation.require(
                dependencies.get(name) == metadata,
                "runtime layer changed SWSS payload: " + name,
            )
    protobuf = artifact_validation.payload(paths["protobuf.tar"], require_root=False)
    package_contract.source_protobuf_contract(dependencies, protobuf)
    debug = artifact_validation.debug_archives(
        paths["rdeps.tar"], paths["debug-symbols.tar"], expected_machine=62,
        readelf=readelf, objcopy=objcopy,
    )
    required_debug = set(programs) | {
        "usr/lib/libdashapi.so",
        "usr/lib/python3/dist-packages/dash_api/_utils.so",
        package_contract.PROTOBUF_RUNTIME,
    }
    artifact_validation.require(
        required_debug <= {pair["path"] for pair in debug["debug_pairs"]},
        "incomplete SWSS/DASH/protobuf debug coverage",
    )
    configuration = artifact_validation.payload(paths["config.tar"])
    artifact_validation.require(
        configuration.get("usr/bin/docker-init.sh", {}).get("mode") == 0o755,
        "missing executable rendered SWSS entrypoint",
    )
    return {
        "programs": programs,
        **debug,
        "source_contract": {
            name: artifact_validation.sha(source / name)
            for name in package_contract.SWSS_CONTRACT_INPUTS
        },
        "scope": "Package payload, architecture, build IDs, DWARF and debuglink CRC; no container or boot execution.",
    }


class SwssPackageContractTest(unittest.TestCase):
    def test_actual_source_layers_and_matching_symbols(self):
        """Reject missing programs, changed data/modes, foreign ELF or stale symbols."""
        with tempfile.TemporaryDirectory(prefix="swss-install-contract-") as temporary:
            source = Path(temporary)
            for relative, origin in (
                ("dist/BUILD.bazel", OPTIONS.install_build),
                ("debian/swss.install", OPTIONS.debian_install),
            ):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)

            # rootpath arguments preserve the source owner's directory structure.
            # Avoid resolve(): that would escape runfiles through Bazel symlinks.
            owner_root = Path(os.path.abspath(OPTIONS.source_anchor)).parent.parent
            copied = set()
            for name in OPTIONS.source_data:
                origin = Path(os.path.abspath(name))
                relative = origin.relative_to(owner_root)
                self.assertNotIn(relative, copied, "duplicate declared install source")
                copied.add(relative)
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)

            validation = verify_packages(
                dict(zip(
                    ("swss.tar", "protobuf.tar", "rdeps.tar", "config.tar", "debug-symbols.tar"),
                    map(Path, (OPTIONS.swss, OPTIONS.protobuf, OPTIONS.rdeps,
                               OPTIONS.config, OPTIONS.debug_symbols)),
                )),
                source,
                readelf=os.path.abspath(OPTIONS.readelf),
                objcopy=os.path.abspath(OPTIONS.objcopy),
            )

        # Bazel exposes this through the build-event report (the CI runner uses
        # --nozip_undeclared_test_outputs), without a container-specific callback.
        outputs = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
        outputs.mkdir(parents=True, exist_ok=True)
        (outputs / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for argument in ("swss", "protobuf", "rdeps", "config", "debug-symbols",
                     "install-build", "debian-install", "source-anchor", "readelf", "objcopy"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--source-data", nargs="+", required=True)
    OPTIONS = parser.parse_args()
    unittest.main(argv=[__file__])
