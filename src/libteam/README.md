# Shared libteam source owner

Bazel builds libteam through the shared `libteam@1.31.0.sonic.1` module. This
directory retains compatibility aliases and installed-runtime validation; the
upstream archive, SONiC patches, compiler inputs, runtime/debug TARs and development
SDKs belong to that module. Make continues to use this directory's existing
Makefile and patch series.

The shared SWSS image installs `@libteam//:libteam_runtime` and
`@libteam//:libteamdctl_runtime` directly so their debug providers remain visible.
Its package records use `@libteam//:library_runtime_packages.json`, which describes
only those two libraries. No utility package payload is added to that image.

CI selects the real owner test `@libteam//:consumer_test`; an alias is useful for
build compatibility but is not a test-suite selection. The native job also checks
the installed SDKs and resolves the exported `@libteam//:bazel/verify_rebuild.py`
through the same Bazel wrapper and execution root used for its initial build.

`tools/bazel/tests/libteam_owner_test.py` compares Make's active patch order
and applied diff bytes with the pinned shared-owner recipe. Registry-only purpose
and provenance headers do not change those diff bytes. The workflow retains the
actual resolved module lock separately; the fixture is a source-parity check,
not evidence that an image was built.
