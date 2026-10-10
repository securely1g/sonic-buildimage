"""Syncd contract tests and Make manifest settings for shared container CI.

The imported native packages and complete OCI images are validated separately;
this profile deliberately selects only tests that need no built package inputs.
"""

from tools.bazel.ci.container import Config

TARGET_NAMES = [
    "syncd_manifest_labels_test",
    "syncd_package_state_layer_test",
    "syncd_select_apt_payloads_test",
    "syncd_package_contract_test",
    "syncd_validate_image_test",
    "syncd_validate_native_packages_test",
    "syncd_validate_payloads_test",
    "apt_selection_test",
]
TESTS = ["//tools/bazel/tests:" + name for name in TARGET_NAMES]

SOURCE_FILES = (
    "MODULE.bazel",
    "BUILD.bazel",
    "platform/vs/docker-syncd-vs.mk",
    "dockers/docker-syncd-vs/BUILD.bazel",
    "dockers/docker-syncd-vs/config/BUILD.bazel",
    "dockers/docker-syncd-vs/config/package_state_layer.py",
    "dockers/docker-syncd-vs/config/runtime_package_state.json",
    "dockers/docker-syncd-vs/config/source_packages.json",
    "tools/bazel/oci/BUILD.bazel",
    "tools/bazel/oci/apt_selection.py",
    "tools/bazel/oci/apt_layer.bzl",
    "tools/bazel/oci/source_modules.bzl",
    "tools/bazel/ci/syncd_image.py",
    "tools/bazel/ci/syncd_native_packages.py",
    "tools/bazel/ci/syncd_payloads.py",
    "tools/bazel/tests/BUILD.bazel",
) + tuple("dockers/docker-syncd-vs/bazel/" + name for name in (
    "BUILD.bazel", "apt.lock.json", "apt_inputs.MODULE.bazel", "package_contract.py",
))


CONFIG = Config(
    name="Syncd OCI contracts",
    scope="Safe contract and manifest tests; no production OCI image, native predecessor, or Bazel DEB-producing target executed.",
    tests=tuple(TESTS),
    make_args=(
        "MANIFEST_METADATA=platform/vs/docker-syncd-vs.mk", "DEBS_PATH=target/debs/trixie",
        "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs",
        "CONFIGURED_ARCH=amd64", "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y", "ENABLE_ASAN=n",
        "ENABLE_SYNCD_RPC=n", "DBG_IMAGE_MARK=dbg", "PLATFORM_PATH=platform/vs", "DOCKERS_PATH=dockers",
    ),
    manifests=("docker-syncd-vs", "docker-syncd-vs-dbg"),
    source_files=SOURCE_FILES,
)
