# Comparing deployment tars with Make packages

Declare each Bazel runtime or debug tar's existing Make counterpart with one
`make-deb:<filename.deb>` tag in the module's root `BUILD.bazel`. For example:

```starlark
filegroup(
    name = "sysmgr_runtime_equivalence",
    srcs = [":sysmgr_pkg"],
    tags = ["make-deb:sysmgr_1.0.0_amd64.deb"],
)
```

Tag a dedicated filegroup when a packaging macro propagates tags to internal
targets. Each tagged target must produce exactly one tar, and each Make filename
must have exactly one target. Put the debug mapping on its final debug tar target,
using `make-deb:sysmgr-dbg_1.0.0_amd64.deb` for sysmgr. The existing
`no-elf-equivalence` exclusion tag still applies.

Expose nested archive targets through a root filegroup. Discovery queries only
that public package so dependency-only builds do not load unrelated developer
tools whose development dependencies are unavailable in the root module graph.

The collector builds only explicitly mapped tars and container images. It reads
the matching Make package from `target/debs/<release>/<filename.deb>` and uses
`dpkg-deb -x` only to extract that existing input. It creates no DEB packages.
Both payloads then use the same installed-path, file, symlink, and ELF comparison
logic. Detached debug information from the separate archives remains paired with
its runtime binary by each build's build ID. Debian control metadata is outside
this payload comparison.
