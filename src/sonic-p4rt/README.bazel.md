# P4RT source build and debug checks

The existing Make recipe builds `//p4rt_app:p4rt_deb` and
`//p4rt_app:p4rt_dbg_deb`, runs `//p4rt_app/...` tests, and publishes the
runtime and debug Debian packages. The legacy Dockerfile installs those
packages through its existing Debian package flow.

For example, an AMD64/Trixie build produces
`target/debs/trixie/sonic-p4rt_0.0.1_amd64.deb` and
`target/debs/trixie/sonic-p4rt-dbgsym_0.0.1_amd64.deb`. Make registers the
runtime package as the Docker runtime dependency and the debug package as
the debug-image dependency. Source-owned TAR targets remain available for
Bazel consumers; legacy Docker assembly does not consume them.

The schema/cache adapter stages the installed `/usr/include/swss/cfg_schema.h`
in a declared repository and supplies its label to Common. The build reuses
`/bazel_cache/repository_cache` and `/bazel_cache/disk_cache`, while keeping
Bazel server/output state private to each slave container.

Run `tools/bazel/ci/verify_p4rt_debug.py` with paired `--runtime-deb` and
`--debug-deb` arguments, plus `--source-root` and `--output`, to check existing
package outputs. Its paired TAR interface remains available to validate source
archives independently of Make/Docker integration.

`p4rt_schema_cache_test.py` exercises the existing package recipe with
controlled Bazel commands, including schema/cache inputs, no-op behavior,
missing-header errors and Docker package selection. `p4rt_tar_test.py` checks
archive inventory validation and the debug verifier's DEB interface.
`p4rt_debug_test.py` compiles native ELF/DWP fixtures and checks isolated GDB
lookup. These focused tests do not establish production P4RT compilation,
full image assembly or forwarding on either architecture.
