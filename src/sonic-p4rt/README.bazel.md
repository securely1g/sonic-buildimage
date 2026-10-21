# P4RT runtime and debug archives

`P4RT_BUILD_WITH_BAZEL_TARS=y` selects the source owner's existing
`//p4rt_app:p4rt_binaries` and `//p4rt_app:p4rt_debug` TAR targets for the Make
container build. The default is `n`, which preserves the existing Debian
package recipes, output names and container dependencies. The outer Make
builder forwards the selector into the slave container.

The selected archives are published as
`target/files/trixie/sonic-p4rt_0.0.1_amd64.tar.bz2` and
`target/files/trixie/sonic-p4rt-dbgsym_0.0.1_amd64.tar.bz2` for AMD64/Trixie.
Runtime and debug images consume these TAR bytes directly. The six runtime
programs retain their `/usr/local/bin` paths; the debug image adds the matching
`p4rt.dwp`. Common and GMP runtime dependencies remain explicit. The file names
separate retained TAR outputs from the legacy DEBs when switching producers.

Each archive is a separate Make target. Requesting debug also prepares runtime,
and removing either archive rebuilds that output. Before executing Bazel, the
TAR recipe resolves only test labels and audits the configured TAR/test action
graph. Query failures, empty test selections and DEB-producing actions stop the
recipe. A failed build does not replace an existing published archive.

For existing TAR outputs, run the same isolated debug check used by the fixture
suite with `--runtime-tar`, `--debug-tar`, `--source-root` and `--output` arguments
to `tools/bazel/ci/verify_p4rt_debug.py`. The verifier also retains the existing
paired `--runtime-deb`/`--debug-deb` interface for legacy workflows.

The PR-triggered `Container archive` jobs run `p4rt_tar_test.py` on AMD64 and
ARM64 through the existing Python test discovery. Those tests exercise Make
selection, missing-output recovery, failure propagation, action auditing,
template rendering and legacy compatibility with controlled Bazel commands.
`p4rt_debug_test.py` separately compiles small native ELF/DWP fixtures and checks
isolated GDB lookup. Neither suite establishes production P4RT compilation,
actual image assembly or forwarding. Those checks remain required for the
selected source revision and architecture. The default full-VS workflow keeps
its existing package path and manual execution guard.

The schema/cache adapter stages the installed `cfg_schema.h` in a declared
repository and supplies its label to Common in both selections. The TAR path
retains `/bazel_cache/repository_cache` and `/bazel_cache/disk_cache` while
keeping Bazel server/output state private to each slave container. The
`p4rt_schema_cache_test.py` suite checks these inputs and no-op behavior.
