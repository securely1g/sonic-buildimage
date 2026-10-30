# Sourced only by the opt-in native VS host stage after onie-image.conf.
SONIC_BAZEL_SNAPSHOT_HELPER=tools/bazel/image/native/snapshot.py
SONIC_BAZEL_STATE_FILE=.sonic-bazel-host-state.json

[[ ${SONIC_BAZEL_HOST_FINALIZE:-n} != y ]] || die "Cannot produce and finalize a host snapshot together"
[[ "$CONFIGURED_ARCH" == amd64 && "$CONFIGURED_PLATFORM" == vs &&
   "$TARGET_MACHINE" == vs && "$IMAGE_DISTRO" == trixie &&
   "$IMAGE_TYPE" == onie && "$RFS_SPLIT_LAST_STAGE" == y &&
   "${RFS_SPLIT_FIRST_STAGE:-n}" == n ]] || die "Unsupported Bazel native host configuration"
[[ "${MULTIARCH_QEMU_ENVIRON:-n}" == n && "${CROSS_BUILD_ENVIRON:-n}" == n ]] || die "Bazel native host requires a native AMD64 build"
[[ "$SONIC_BAZEL_SOURCE_COMMIT" == "$(git rev-parse HEAD)" &&
   "$SONIC_BAZEL_SOURCE_BRANCH" == "$(git rev-parse --abbrev-ref HEAD)" ]] || die "Bazel native host source identity changed"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ && -n "$SONIC_IMAGE_VERSION" ]] || die "Bazel native host requires fixed version and epoch"
[[ -x sonic_debian_extension.sh ]] || die "Bazel native host requires rendered service templates"
[[ "$SONIC_BAZEL_HOST_SNAPSHOT" == "$PWD/target/bazel-native/host-onie.squashfs" &&
   -d target/bazel-native && ! -L target/bazel-native &&
   ! -e "$SONIC_BAZEL_HOST_SNAPSHOT" && ! -L "$SONIC_BAZEL_HOST_SNAPSHOT" ]] || die "Bazel native snapshot requires a fresh output path"
sudo python3 "$SONIC_BAZEL_SNAPSHOT_HELPER" assert-clean "$FILESYSTEM_ROOT"

sonic_bazel_write_host_snapshot()
(
    set -e
    local state_tmp="" snapshot_tmp=""
    cleanup_snapshot_files()
    {
        [[ -z "$state_tmp" ]] || rm -f -- "$state_tmp"
        [[ -z "$snapshot_tmp" ]] || sudo rm -f -- "$snapshot_tmp"
        sudo rm -f -- "$FILESYSTEM_ROOT/$SONIC_BAZEL_STATE_FILE"
    }
    trap cleanup_snapshot_files EXIT
    sudo python3 "$SONIC_BAZEL_SNAPSHOT_HELPER" quiesce "$FILESYSTEM_ROOT"
    sudo chroot "$FILESYSTEM_ROOT" rm -rf -- /run/docker /run/containerd /run/docker.pid /run/docker-ssd.pid /run/docker.sock
    state_tmp=$(mktemp)
    python3 "$SONIC_BAZEL_SNAPSHOT_HELPER" write-state "$state_tmp" \
        --arch "$CONFIGURED_ARCH" --platform "$CONFIGURED_PLATFORM" --machine "$TARGET_MACHINE" \
        --image-type "$IMAGE_TYPE" --distro "$IMAGE_DISTRO" --image-version "$SONIC_IMAGE_VERSION" \
        --source-commit "$SONIC_BAZEL_SOURCE_COMMIT" --source-branch "$SONIC_BAZEL_SOURCE_BRANCH" \
        --source-date-epoch "$SOURCE_DATE_EPOCH"
    sudo install -m 0644 "$state_tmp" "$FILESYSTEM_ROOT/$SONIC_BAZEL_STATE_FILE"
    snapshot_tmp=$(mktemp "${SONIC_BAZEL_HOST_SNAPSHOT}.tmp.XXXXXX")
    rm -f -- "$snapshot_tmp"
    sudo mksquashfs "$FILESYSTEM_ROOT" "$snapshot_tmp" -comp zstd -b 1M -noappend
    sudo chown "$(id -u):$(id -g)" "$snapshot_tmp"
    mv -T -- "$snapshot_tmp" "$SONIC_BAZEL_HOST_SNAPSHOT"
)
