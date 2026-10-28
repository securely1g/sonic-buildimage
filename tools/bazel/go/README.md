# Go module download cache

`MODULE.bazel` selects the patched Gazelle and declares this root's ZIP lock.
Update dependencies in their owning Go modules, then run from buildimage:

```sh
tools/bazel/go-lock
tools/bazel/go-lock --check
```

`BAZEL=/path/to/bazel` selects the Bazel command. The shared updater in
`sonic-build-infra/go/lock.py` uses the rules_go SDK selected by this entry.
Review and commit the Go declarations and ZIP lock together. Ordinary builds
consume reviewed digests; missing entries fail with an update instruction.

The independent lemming workspace exports the same plan alias and owns its
`go.zip.sums.json`. Update that graph separately:

```sh
tools/bazel/go-lock --workspace "$PWD/platform/alpinevs/src/libsai-grpc/lemming"
```

Every additional isolated extension needs an imported plan and an additional
`--plan` argument. Explicit `go_repository` declarations with `zip_sums` export
plans through the same interface. The shared updater does not scan Starlark.

Bazel module resolution, source archive caching, and compiled-action caching
remain separate. Module ZIP caching requires `--repository_cache` and/or
`--experimental_remote_downloader`; `--remote_cache` alone caches build actions.
Ordinary ZIP downloads support one HTTP(S) `GOPROXY`. See the shared updater's
README for private modules, proxy policy and failure behavior.

This Draft selects the reviewed registry candidate recorded in
`.bazelrc`. It contains Gazelle's ZIP-cache extension and the
source versions providing the updater and Common's declared-runtime tests.
After those registry/source PRs merge, restore the maintained registry endpoint
and refresh resolution before promoting the integration PR.

The independent Lemming checkout retains Alpine's existing runtime source and
owns a 200-module lock. Its Bazel 8.4.1 / Go 1.26.6 entry is checked separately
from buildimage's Bazel 8.5.1 entry. A Bazelisk command selects the version for
each workspace automatically; an explicit `BAZEL` override must match that entry.
