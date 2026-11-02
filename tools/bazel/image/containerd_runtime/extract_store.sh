#!/bin/sh
# Restore a Bazel-prepared store with the shipped host's GNU tar, including
# trusted.overlay.opaque. ONIE's tar may be BusyBox and lack xattr support.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: extract_store.sh FS_SQUASHFS DESTINATION < dockerfs.tar.gz" >&2
    exit 2
fi
filesystem=$1
destination=$2
[ -f "$filesystem" ]
[ -d "$destination" ]
target_root=$(mktemp -d "${TMPDIR:-/tmp}/sonic-containerd-extract.XXXXXX")
root_mounted=false
store_mounted=false

unmount_path()
{
    # The last chroot reference can outlive tar briefly in the kernel. Retry a
    # normal unmount; never hide a still-busy mount with lazy detachment.
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        if umount "$1"; then
            return 0
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -lt 5 ]; then
            sleep 1
        fi
    done
    return 1
}

cleanup()
{
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$store_mounted" = true ]; then
        unmount_path "$target_root/tmp" || status=1
    fi
    if [ "$root_mounted" = true ]; then
        unmount_path "$target_root" || status=1
    fi
    rmdir "$target_root" || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mount -t squashfs -o loop,ro "$filesystem" "$target_root"
root_mounted=true
# /var/lib/docker is excluded from fs.squashfs; /tmp is an existing mountpoint.
mount --bind "$destination" "$target_root/tmp"
store_mounted=true
# The shipped zstd supports gzip as well as zstd streams. Let it identify the
# compression from stdin, without requiring either decoder in the ONIE image.
chroot "$target_root" /usr/bin/tar --extract --use-compress-program=/usr/bin/zstd --file=- --directory=/tmp \
    --numeric-owner --same-permissions --xattrs --xattrs-include='*' --acls
