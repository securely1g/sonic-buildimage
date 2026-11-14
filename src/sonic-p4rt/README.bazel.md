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

The verifier also accepts existing source-owner TAR outputs with paired
`--runtime-tar` and `--debug-tar` options. This standalone validation support
does not register those outputs with Make or feed them into legacy Docker.
Both interfaces require `--source-root` and `--output`.

`tools/bazel/tests/p4rt_tar_test.py` checks standalone archive validation and
legacy DEB selection. `p4rt_debug_test.py` compiles native ELF/DWP fixtures and
checks isolated GDB lookup, including invalid and unrelated symbols. These
fixture checks do not establish production P4RT compilation, image assembly or
forwarding for a given source revision and architecture.

The source-image SDK handoff gives the separate Bazel 6 P4RT owner the same
libyang library used by its config-engine parent. It collects the existing
`@libyang//:libyang_pkg` runtime TAR, checks the AMD64
`libyang.so.3.9.1` bytes and relative `libyang.so.3` alias against the parent
TAR, then declares the SONAME through `cc_import`. This replaces the ambient
`-lyang` search only for that image handoff. The standalone Common SDK keeps
its existing slave dependency contract.

Synthetic cases in `tools/bazel/ci/p4rt_image_test.py` cover declared linkage,
parent-byte drift, wrong architecture, missing inputs, unsafe aliases, and
preservation of the previous SDK on rejection. These pure checks do not prove
the native owner link or image runtime; those remain part of the image CI run.

The image SDK preserves the four libnl development aliases from the declared
owner SDK TARs (`libnl*.so -> libnl*.so.200`) and uses those linker names in its
existing `cc_import` targets. The pinned P4RT owner's `receive_genetlink` rule
also supplies `-lnl-*` options, so importing only the versioned SONAME leaves
its `-lnl-nf-3` input unresolved. Each alias must point to the exact validated
SONAME; the real library bytes remain bound to the selected parent image.
Standalone legacy SDK behavior and the owner checkout are unchanged. Guarded
synthetic tests cover these paths; native link acceptance belongs to CI.
