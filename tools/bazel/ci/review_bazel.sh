#!/usr/bin/env bash
# Review the complete immutable catalog without changing normal .bazelrc.
# Generalizes PR #136's libteam_draft_bazel.sh for image consumers.
set -euo pipefail
bazel_real=${BAZEL_REAL:-bazel}
if [[ $# == 1 && "$1" == --version ]]; then
    exec "$bazel_real" version --gnu_format
fi
startup=()
while [[ $# -gt 0 && "$1" == --* ]]; do
    case "$1" in
        --bazelrc|--bazelrc=*|--*system_rc|--*system_rc=*|--*home_rc|--*home_rc=*|--*workspace_rc|--*workspace_rc=*|--ignore_all_rc_files*)
            printf '%s\n' 'Review builds select their own rc files.' >&2; exit 2 ;;
        --output_base|--output_user_root|--host_jvm_args|--max_idle_secs|--io_nice_level|--connect_timeout_secs|--server_javabase|--install_base)
            [[ $# -ge 2 ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            startup+=("$1" "$2"); shift 2 ;;
        *) startup+=("$1"); shift ;;
    esac
done
bazel_command=${1:-}
if [[ "$bazel_command" == shutdown ]]; then
    [[ $# == 1 ]] || { printf '%s\n' 'shutdown does not accept build arguments.' >&2; exit 2; }
else
    revision=${SONIC_BAZEL_REGISTRY_REVISION:-}
    if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
        printf '%s\n' 'SONIC_BAZEL_REGISTRY_REVISION must be an immutable 40-character lowercase Git commit.' >&2
        exit 2
    fi
    case "$bazel_command" in
        aquery|build|cquery|fetch|info|mod|query|test) shift ;;
        *) printf 'Unsupported review Bazel command: %s\n' "$bazel_command" >&2; exit 2 ;;
    esac
    for argument in "$@"; do
        case "$argument" in
            --registry|--registry=*) printf '%s\n' 'Select the review registry through SONIC_BAZEL_REGISTRY_REVISION.' >&2; exit 2 ;;
        esac
    done
fi
rc=$(mktemp "${TMPDIR:-/tmp}/sonic-review-bazel.XXXXXXXX")
trap 'rm -f -- "$rc"' EXIT
python3 "$(dirname "$0")/review_bazel_rc.py" "$rc"
bazel=("$bazel_real" "${startup[@]}" --nosystem_rc --nohome_rc --noworkspace_rc --bazelrc="$rc")
if [[ "$bazel_command" == shutdown ]]; then
    # Use the same rc/startup selection as info/build. Reading the ambient home
    # rc here can select setup-bazel's different output_base and leave our JVM
    # running. Shutdown needs neither a registry revision nor build arguments.
    "${bazel[@]}" shutdown
else
    "${bazel[@]}" "$bazel_command" \
        --registry="https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/$revision" \
        --registry=https://bcr.bazel.build "$@"
fi
