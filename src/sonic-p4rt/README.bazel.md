# P4RT packages and debug validation

The existing Make recipe builds `//p4rt_app:p4rt_deb` and
`//p4rt_app:p4rt_dbg_deb` from the pinned `sonic-pins` source. Legacy Docker
builds install those DEBs. `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y` selects Bazel
only for registered OCI images; it does not change P4RT's package format or
inject source TARs into its Dockerfile.

For example, the full VS workflow requests both
`target/debs/trixie/sonic-p4rt_0.0.1_amd64.deb` and
`target/debs/trixie/sonic-p4rt-dbgsym_0.0.1_amd64.deb`, then verifies their
split debug symbols with `tools/bazel/ci/verify_p4rt_debug.py` using the paired
`--runtime-deb` and `--debug-deb` options.

The Make recipe stages the installed `/usr/include/swss/cfg_schema.h` under
`/tmp/sonic-p4rt-cfg-schema`, exports it as a Bazel repository, and passes its
label to Common. A missing header fails before Bazel starts. The P4RT and
Common submodules use pinned source from their maintained `securely1g` forks.

The standard `/bazel_cache` mount supplies `repository_cache` and `disk_cache`.
Bazel server state and output directories stay private to each slave container.

The verifier also accepts existing source-owner TAR outputs with paired
`--runtime-tar` and `--debug-tar` options. This standalone validation support
does not register those outputs with Make or feed them into legacy Docker.
Both interfaces require `--source-root` and `--output`.

`tools/bazel/tests/p4rt_schema_cache_test.py` runs the Make recipe with controlled
Bazel commands to check schema/cache inputs, missing-header failure, and no-op
output preservation. `p4rt_tar_test.py` checks standalone archive validation and
legacy DEB selection. `p4rt_debug_test.py` compiles native ELF/DWP fixtures and
checks isolated GDB lookup, including invalid and unrelated symbols. These
fixture checks do not establish production P4RT compilation, image assembly or
forwarding for a given source revision and architecture.
