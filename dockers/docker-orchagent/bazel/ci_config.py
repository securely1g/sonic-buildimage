"""SWSS source-layer targets and package checks for the shared container CI runner."""

from pathlib import Path

from tools.bazel.ci import artifact_validation, build
from tools.bazel.ci.container import Config, load_module

package_contract = load_module(Path(__file__).with_name("package_contract.py"))

# These targets create tar archives, never Debian packages. Make supplies the
# config-engine base and Scapy wheel when it assembles the complete OCI image.
TARGETS = {
    "swss.tar": "@sonic_swss//dist:swss_pkg",
    "protobuf.tar": "@sonic_dash_api//:protobuf_runtime_pkg",
    "rdeps.tar": "//dockers/docker-orchagent:rdeps",
    "config.tar": "//dockers/docker-orchagent/config:files",
    "debug-symbols.tar": "//dockers/docker-orchagent:rdeps_debug_symbols",
}
TESTS = [
    "//tools/bazel/tests:apt_selection_test",
    "//tools/bazel/tests:select_apt_payloads_test",
    "//tools/bazel/tests:sonic_py_common_test",
    "//tools/bazel/tests:sonic_py_common_full_test",
    "//tools/bazel/tests:sonic_config_engine_full_test",
    "//tools/bazel/tests:sonic_python_wheels_test",
    "//src/sonic-config-engine:sonic_cfggen_cli_test",
    "@sonic_swss//crates/countersyncd:common_rust_test",
    "//tools/bazel/tests:test_dpkg_patterns_up_to_date",
    "//tools/bazel/tests:manifest_labels_test",
    "//tools/bazel/tests:manifest_labels_action_test",
    "//tools/bazel/tests:swss_render_test",
]


def verify_packages(paths, source):
    runtime = artifact_validation.payload(paths["swss.tar"])
    programs = package_contract.swss_contract(source, runtime)
    dependencies = artifact_validation.payload(paths["rdeps.tar"])
    for name, metadata in runtime.items():
        if metadata["kind"] != "directory":
            artifact_validation.require(dependencies.get(name) == metadata, "runtime layer changed SWSS payload: " + name)
    protobuf = artifact_validation.payload(paths["protobuf.tar"], require_root=False)
    package_contract.source_protobuf_contract(dependencies, protobuf)
    debug = artifact_validation.debug_archives(
        paths["rdeps.tar"], paths["debug-symbols.tar"], expected_machine=62)
    required_debug = set(programs) | {"usr/lib/libdashapi.so", "usr/lib/python3/dist-packages/dash_api/_utils.so", package_contract.PROTOBUF_RUNTIME}
    artifact_validation.require(required_debug <= {pair["path"] for pair in debug["debug_pairs"]}, "incomplete SWSS/DASH/protobuf debug coverage")
    configuration = artifact_validation.payload(paths["config.tar"])
    artifact_validation.require(configuration.get("usr/bin/docker-init.sh", {}).get("mode") == 0o755,
                     "missing executable rendered SWSS entrypoint")
    return {"programs": programs, **debug,
            "source_contract": {name: artifact_validation.sha(source / name)
                                for name in package_contract.SWSS_CONTRACT_INPUTS},
            "scope": "Package payload, architecture, build IDs, DWARF and debuglink CRC; no container or boot execution."}


def source_directory(workspace, bazel, options, directory, receipt):
    source = build.source_directory(workspace, directory, receipt, TARGETS["swss.tar"],
                                    bazel=bazel, options=options, name="swss-source")
    if not (source / "dist/BUILD.bazel").is_file():
        raise ValueError("Resolved SWSS source lacks its install declarations: " + str(source))
    return source


def validate_archives(workspace, paths, directory, receipt, *, bazel, options):
    source = source_directory(workspace, bazel, options, directory, receipt)
    return verify_packages(paths, source)


CONFIG = Config(
    name="SWSS source layers",
    scope="SWSS source layers and matching debug symbols; no complete OCI image or VS installer.",
    tests=tuple(TESTS),
    archives=TARGETS,
    make_args=(
        "MANIFEST_METADATA=rules/docker-orchagent.mk",
        "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs",
        "CONFIGURED_ARCH=amd64", "ENABLE_ASAN=n", "DBG_IMAGE_MARK=dbg", "DOCKERS_PATH=dockers",
    ),
    manifests=("docker-orchagent", "docker-orchagent-dbg"),
    source_files=("dockers/docker-orchagent/bazel/package_contract.py",),
    validate_archives=validate_archives,
    retain_test_events=True,
)
