# Native AMD64 MACsec image

This composition keeps the selected Trixie Dockerfile contract. The shared
`docker-swss-layer-trixie` parent already supplies macsecmgrd, Common, OpenSSL,
all five source-built libnl runtime packages and the supervisor utilities.
The child consumes WPA's public `wpasupplicant_pkg` TAR and matching symbols;
it does not rebuild those shared libraries or create Debian packages.

The six Dockerfile-owned supervisor, WPA and CLI files retain their exact source
bytes and 0644/root installation modes. The installed entrypoint remains
`/usr/local/bin/supervisord`, with rsyslogd preceding macsecmgrd. Make's original
prerequisites, service metadata, host CLI hooks and volume flags remain in place;
the Bazel handoff applies only to native AMD64 VS/Trixie with the ordinary `dbg`
marker and without ASAN, cross compilation or QEMU.

The external APT inputs are pinned to the parent's existing Debian snapshots.
Runtime roots are libdbus-1-3, libpcsclite1, libreadline8t64, libtinfo6, lsb-base
and adduser. Common, native libnl and OpenSSL continue to come from their source
owners. The checked APT selector retains matching parent packages and verifies
file conflicts. The source metadata records only the actual WPA package; it
does not invent records for unrelated inherited artifacts.

The installed fixture uses a private Redis instance with both the shipped Unix
socket and a loopback TCP listener, a private veth pair, the actual macsecmgrd
executable and its actual WPA child. It checks the POST and port readiness gates,
then observes the SONiC driver's real MACSEC_PORT_TABLE request. It deliberately
stops before an orchagent/SAI response. This is a software handoff test, not an
ASIC, encrypted-traffic, FIPS POST or installer-boot result. Both image variants
must also pass exact source-payload checks, complete native loader checks and
selected GDB checks. The Redis process is test-only and is not added to the image.

The WPA compiler-flag transport is supplied by [source PR #2](https://github.com/securely1g/sonic-wpa-supplicant/pull/2) through [registry #210](https://github.com/securely1g/sonic-bazel-registry/pull/210). Its ordered response file prevents the complete native include list from exceeding the Linux environment-entry limit before Make starts. [Infrastructure #209](https://github.com/securely1g/sonic-bazel-registry/pull/209) supplies the cumulative shared configure transport. Image build, loaded-service and incremental acceptance remain separate checks.

Review builds use `tools/bazel/ci/review_bazel.sh` from [PR #146](https://github.com/securely1g/sonic-buildimage/pull/146) with `SONIC_BAZEL_REGISTRY_REVISION=2c9a1bfed7c4e5db33bb7279ea10d055a17eff29`. The ordinary `.bazelrc` retains maintained main plus BCR. The inherited `libteam_draft_bazel.sh` entry point only delegates to that shared helper with the same complete catalog; it has no separate resolution logic. The build receipt records the selected review revision and Bazel invocation.
