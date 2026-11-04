# Go module download cache

`MODULE.bazel` selects the patched Gazelle and declares this root's ZIP lock.
The committed `.bazelrc` keeps the maintained registry default. The selected
Gazelle, build-infra and Common versions are not available there yet, so use
the explicit Draft configuration when validating this integration. Update
dependencies in their owning Go modules, then run from buildimage:

```sh
BAZEL="$PWD/tools/bazel/go/bazel-with-draft-registry" tools/bazel/go-lock
BAZEL="$PWD/tools/bazel/go/bazel-with-draft-registry" tools/bazel/go-lock --check
```

`BAZEL=/path/to/bazel` selects the command used by the entry point and shared
updater. `GO_CACHE_BAZEL=/path/to/bazel` selects the underlying Bazel command
when using `bazel-with-draft-registry`. The shared updater in
`sonic-build-infra/go/lock.py` uses the rules_go SDK selected by this entry.
Review and commit the Go declarations and ZIP lock together. Ordinary builds
consume reviewed digests; missing entries fail with an update instruction.

The independent lemming workspace exports the same plan alias and owns its
`go.zip.sums.json`. Update that graph separately:

```sh
BAZEL="$PWD/tools/bazel/go/bazel-with-draft-registry" tools/bazel/go-lock \
  --workspace "$PWD/platform/alpinevs/src/libsai-grpc/lemming"
```

Every additional isolated extension needs an imported plan and an additional
`--plan` argument. Explicit `go_repository` declarations with `zip_sums` export
plans through the same interface. The shared updater does not scan Starlark.

Bazel module resolution, source archive caching, and compiled-action caching
remain separate. Module ZIP caching requires `--repository_cache` and/or
`--experimental_remote_downloader`; `--remote_cache` alone caches build actions.
Ordinary ZIP downloads support one HTTP(S) `GOPROXY`. See the shared updater's
README for private modules, proxy policy and failure behavior.

`bazel-with-draft-registry` disables the root workspace rc file and selects
`draft.bazelrc`, which contains the single reviewed registry candidate
`36bf3d2becfd3552aae58dd68244cb479e4fa8b7` plus BCR. It preserves system and
home rc files used by CI caches. The candidate contains Gazelle's ZIP-cache
extension and the source versions providing the updater and Common's
declared-runtime tests. Use the wrapper directly for other root checks, such as
`tools/bazel/go/bazel-with-draft-registry test //tools/bazel/tests:swss_common_rust_test`.
Inspect `--announce_rc` output when adding local rc files so they do not add a
second endpoint for the same registry. Remove this temporary configuration and
its CI selections after the source and registry entries land, then refresh
resolution through the maintained registry.

The independent Lemming checkout retains Alpine's existing runtime source and
owns a 200-module lock. Its Bazel 8.4.1 / Go 1.26.6 entry is checked separately
from buildimage's Bazel 8.5.1 entry. A Bazelisk command selects the version for
each workspace automatically; an explicit `BAZEL` override must match that entry.
The wrapper applies the root candidate only in the buildimage module and leaves
nested workspaces on their own configuration. The selected Lemming source still
uses its separate temporary catalog; its owner must restore that default when
its dependencies land.
