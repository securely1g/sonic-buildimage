# Target runtime for the Bazel containerd store

This runtime is selected only by `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y`.
Default Bazel builds keep Docker/overlay2; native Make remains the default when
`BUILD_WITH_BAZEL_WHEN_AVAILABLE` is unset. See the [build commands and prerequisites](../README.md).

The opted-in build converts service images with Skopeo and imports them through a
private containerd using `ctr images import --no-unpack`. It retains complete
shared compressed blobs, configuration and tags, stops the daemon, and packages
the populated store without unpacked image snapshots. Both OCI layers and the
store archive use zstd level 6; the archive retains SONiC's `dockerfs.tar.gz` name.

`overlay.py --rootfs ROOT --architecture amd64` configures only the opted-in host output.
It preserves SONiC's Docker networking and socket settings, enables Docker's
containerd image store, and selects `/run/containerd/containerd.sock`, namespace
`moby`, snapshotter `overlayfs`. Containerd starts before Docker and keeps its
data under the mounted per-image directory `/var/lib/docker/containerd-store`.
The original containerd configuration remains on disk; a Bazel-only systemd
override selects the dedicated configuration. The validated target versions are
Docker 28.5.2 and containerd 1.7.28.

Docker unpacks each image synchronously when its first container is created.
Later containers reuse the snapshots and shared parent chains. SONiC's existing
Docker service scripts therefore trigger unpacking without a separate first-boot
import or unpack service. A smaller installer trades build-time snapshot storage
for target cold-start work; installed disk usage grows as images are used, while
their compressed content remains available for image inspection and updates.

Archive handling preserves extended attributes, ownership, permissions, device
nodes and ACLs. This also permits restoring older stores containing snapshots,
where losing `trusted.overlay.opaque` could expose files deleted by an upper layer.
For this ONIE image, `extract_store.sh` mounts the shipped `fs.squashfs` and
uses its GNU tar with `--xattrs --xattrs-include='*' --acls --numeric-owner
--same-permissions` and the shipped zstd decoder, which accepts gzip and zstd
streams. This avoids depending on ONIE's tar or compression tools. The overlay
also changes the restored host's generated initramfs extraction script to use
those tools for `docker_inram`. Default Bazel and native Make extraction remain
unchanged.

Full OS-upgrade package migration is not validated: the existing migration
chroot starts Docker through its SysV script, which retains an `overlay2` flag
and does not start this image's private containerd. This is separate from Docker
container-image updates and is tracked in [#74](https://github.com/securely1g/sonic-buildimage/issues/74).

Run the host overlay contract tests with:

```sh
PYTHONPATH=tools/bazel/image/containerd_runtime \
  python3 tools/bazel/image/containerd_runtime/runtime_test.py
```

The separate privileged acceptance fixture exercises the actual importer and
store restoration, first-use unpacking, metadata, Docker startup and restart.
It does not replace a complete SONiC guest boot, service readiness or forwarding
test; simultaneous cold starts and service timeouts also need guest validation.
