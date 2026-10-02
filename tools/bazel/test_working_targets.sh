#!/bin/bash
set -Eeuo pipefail

# This script is long and mostly quiet until something breaks,
# so name the command that failed.
trap 'echo "[FAILED] ${BASH_SOURCE[0]}:${LINENO}: ${BASH_COMMAND}" >&2' ERR

repo_root=$(git rev-parse --show-toplevel)

function run_in_slave() {
  local repo=$1
  local cmd=$2

  # SKIP_SLAVE=1 still runs the command, just on the host. Skipping it outright
  # would make the whole script exit 0 while testing nothing.
  if [[ "${SKIP_SLAVE:-0}" == "1" ]]; then
    echo "[host] ${repo}: ${cmd}"
    (cd "${repo_root}/${repo}" && eval "${cmd}")
    return
  fi

  # Run the same command inside the sonic-slave-trixie container.
  echo "[slave] ${repo}: ${cmd}"
  make -C "${repo_root}" -f Makefile.work BLDENV=trixie sonic-slave-run \
    SONIC_RUN_CMDS="cd /sonic/${repo} && ${cmd}"
}

function test_repo() {
  local repo=$1
  local build_targets=${2//$'\n'/ }
  local test_targets=${3:-}
  test_targets=${test_targets//$'\n'/ }

  echo "[test_repo] ${repo}"
  # Keep required outputs explicit: wildcard builds can select DEB fixtures or
  # silently skip architecture-incompatible runtime packages.
  if [[ -n "${build_targets}" ]]; then
    run_in_slave "${repo}" "bazel build ${build_targets}"
  fi
  if [[ -n "${test_targets}" ]]; then
    run_in_slave "${repo}" "bazel test ${test_targets}"
  fi
  run_in_slave "${repo}" "bazel run //tools/bazel/buildifier:buildifier.check"
}

echo "[= Testing Docker Images =]"

cd "${repo_root}"
python3 -m unittest discover -s tools/bazel/tests -p '*_test.py' -v

# These are the reviewed runtime/debug image paths. Preparing their existing
# Make inputs is independent native work; their Bazel actions produce tar/OCI
# outputs. Do not discover arbitrary new build selections with a wildcard.
docker_images="docker-sysmgr docker-orchagent"

for image in ${docker_images}; do
    # Every Bazel-built container has a paired debug image, and both go through
    # the same sonic_docker_archive chain, so build both.
    for archive in "${image}.gz" "${image}-dbg.gz"; do
      echo "[docker-make] ${archive}"

      rm -f "target/${archive}"
      BAZEL_MIN_READINESS=experimental \
        NOBOOKWORM=1 \
        BLDENV=trixie \
        make "target/${archive}"
    done
done

echo "[= Testing sonic-buildimage =]"

# The CI driver owns the explicit image/controller unit-test list.
run_in_slave "." "python3 tools/bazel/ci/run.py test --artifacts artifacts/working-targets"
run_in_slave "." "bazel test \
  //tools/bazel/registry:root_config_test \
  //tools/bazel/registry:submodule_config_test \
  //tools/bazel/equivalence_checker:rules_engine_test \
  //tools/bazel/equivalence_checker:reporter_test \
  //tools/bazel/equivalence_checker:deployment_tar_test \
  //dockers/docker-sysmgr:debug_symbols_test"

# libnl3 is a registered dependency; it no longer has an in-tree Bazel module.
run_in_slave "." "bazel build \
  @libnl3//:libnl-3_pkg @libnl3//:libnl-genl-3_pkg \
  @libnl3//:libnl-route-3_pkg @libnl3//:libnl-nf-3_pkg \
  @libnl3//:libnl-cli-3_pkg @libnl3//:libnl-3-dev_pkg \
  @libnl3//:libnl-genl-3-dev_pkg @libnl3//:libnl-route-3-dev_pkg \
  @libnl3//:libnl-nf-3-dev_pkg @libnl3//:libnl-cli-3-dev_pkg"

echo "[= Testing Dependent Repositories =]"

test_repo "src/sonic-build-infra" \
  "//tests:hello_deploy_tar //tests:hello_deploy_tar.debug_symbols" \
  "//tests:simple_tar_mutate_assert
   //tests:simple_tar_mtree_assert
   //tests:inconsistent_sizes_assert
   //tests:hello_cpp_build_test
   //tests:hello_build_test
   //tests:hello_strip_test
   //tests:hello_strip_provides_debug_symbols_test
   //tests:cc_toolchain_supports_pic_test
   //tests:libgreet_build_test
   //tests:libgreet_pic_test
   //tests:libgreet_strip_test
   //tests:greet_shared_build_test
   //tests:greet_shared_pic_test
   //tests:greet_shared_strip_test
   //tests:hello_deploy_tar_assert
   //tests:hello_deploy_tar_content_test
   //tests:hello_deploy_tar_debug_assert
   //tests:hello_deploy_tar_provides_debug_symbols_test
   //tests:hello_collected_debug_symbols_assert
   //tests:hello_base_collected_debug_symbols_assert
   //tests:external_deploy_tar_build_test
   //tests:hello_rpath_preservation_test
   //tests:hello_runpath_preservation_test
   //tests:hello_static_preservation_test
   //tests/shared_api_consumer:sysroot_test
   //tests/shared_api_consumer:python_32_files_test
   //tests/shared_api_consumer:python_32_tree_test
   //tests/shared_api_consumer:python_64_files_test
   //tests/shared_api_consumer:python_64_tree_test
   //tests/shared_api_consumer:go_32_files_test
   //tests/shared_api_consumer:go_32_tree_test
   //tests/shared_api_consumer:go_64_files_test
   //tests/shared_api_consumer:go_64_tree_test
   //tar:root_owned_tar_test
   //tar:debug_symbols_ownership_test
   //python:wheel_layer_test
   //proto:protoc_version_test"

test_repo "src/sonic-swss-common" \
  "//dist:libswsscommon_pkg //dist:libswsscommon_pkg.debug_symbols
   //dist:sonic-db-cli_pkg //pyext:swsscommon_pkg //goext:swsscommon" \
  "//tests:status_code_util_test //tests:saiaclschema_ut
   //tests:notification_queue_ut //tests:interface_ut //tests:vrf_ut
   //tests:shared_library_runtime_test //tests:defaultvalueprovider_ut
   //goext:swsscommon_runtime_test //pyext:swsscommon_package_test
   //dist:libswsscommon_package_test"

test_repo "src/sonic-sysmgr" "//:sysmgr_pkg //:sysmgr_debug_pkg"
test_repo "src/sonic-fips" "//:baseimage_installers"
test_repo "src/protobuf" \
  "//:libprotobuf //:libprotobuf_headers //:well_known_protos
   //:descriptor_proto //:protoc //:libprotoc_soname //:libprotoc_library
   //:libprotobuf_soname //:libprotobuf_library //:protoc_version"

echo "[= Testing Binary Equivalence with Make =]"

EQUIVALENCE_ALLOW_DIRTY=1 "${repo_root}/tools/bazel/test_equivalence.sh"

echo "[= DONE =]"
