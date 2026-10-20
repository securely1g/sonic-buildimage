# Retained native diagnostic for run 39383813400

This operational branch provides one opt-in continuation of the failed PR2
native attempt. It does not change PR2, its normal workflow, or the normal
native/package/installer identity contract. Dispatch the existing
`runner-manager.yml` workflow on the reviewed operational branch; its
`native_recovery` input defaults to `off`. Ordinary events still run the hosted
runner lifecycle tests. Every dispatch has its own concurrency group and never
cancels another build.

The pinned attempt used PR2 head
`2546efcc7702088a3f43fa82692002a21bc6d053`, but actually built merge
`c475a443ab42d3a5f9e862f72b33303a9c2769e8` with epoch `1792479658`.
The retained build is `/var/tmp/sonic-vs-installer-63_8m77r/build`.
This is host temporary storage subject to host retention, not a new persistent
cache. The existing `/data` repository/package caches are neither moved nor
deleted. No automatic retained-workspace cleanup is provided.

## Inspect before resume

Dispatch `native_recovery=inspect` with `inspect_capacity=false`. This reports
the actual executing UID/GID, private-parent and retained-directory ownership,
live filesystem/free-space information, and hashes of known prior public
evidence. Each unavailable path has its own error. Inspect creates no lock,
does not start or query Docker, and does not repair permissions or write to the
retained area. It writes only its new job evidence. Uploaded evidence is separate
from the historical allocation receipt; the old 380 GiB figure is not current
admission evidence.

Inspection also inventories the fixed old `native-cache`, `native-cache/bazel`,
and `native-cache/vcache` paths. It reports ownership, modes, host access,
immediate child names, and metadata counts and sizes without opening file
contents. Each inventory is limited to 10,000 entries, three seconds, depth 32,
and 100 reported immediate children. It does not follow symlinks or traverse
another filesystem; permission errors and incomplete counts are explicit.
Regular-file byte totals count hard-linked files once within each inventory.
The three overlapping inventories are separate observations and must not be
summed. Inspect neither runs Docker nor changes owners or permissions. These
observations determine whether the derived caches can be reused before any
change to the diagnostic's cache selection.

Inspection run `39429628567` established runner UID/GID `1001:1003`. Its
SHA256-verified receipt and artifact identities are pinned in the manifest.
Resume requires that exact executing identity and observed mixed ownership:
the private `0700` parent, build root, checkout, state, and target belong to
`1001:1003`; the native and Bazel-source directories remain `1000:1000`; the
outer Docker data root remains `0:0`. A mismatch rejects without repair.
The old native `native-cache` remains owned by `1000:1000`; it is not mounted
as the new `/dpkg_cache`. Make checks that mount's permissions even with cache
method `none`, so the diagnostic supplies a fresh runner-owned cache instead.
After creating the account, the worker checks the fresh cache and its derived
Bazel/version-cache directories as that actual account before starting dockerd
or Make. Host supplementary-group or ACL access cannot incorrectly authorize a
worker that lacks those permissions.
Resume also rechecks a live root-backed, writable ext4 filesystem with at
least 300 GiB available. Inspection measured 303.13 GiB, only 3.13 GiB above
that threshold on the filesystem shared with host Docker; this historical
measurement does not replace admission.

Read-only inspection `39440015581`, artifact `12272974580`, found no file data
in the old derived caches: Bazel was completely empty; vcache contained two
directories and zero files/bytes. All inventories completed without errors or
limits. The artifact and receipt hashes are pinned in the manifest. Before
continuation, bounded inventories must again prove each old derived cache is
missing or complete with no file, symlink, or other data. Populated or incomplete
inventories reject before source changes or worker creation. Disabled DPKG-cache
markers outside those derived directories are preserved without treating them
as Bazel data.

The diagnostic creates `state/native-diagnostic/native-cache` and its `bazel`
and `vcache` children as private runner-owned directories. It replaces exactly
one recorded `SONIC_DPKG_CACHE_SOURCE` argument; both `SONIC_DPKG_CACHE_METHOD=none`
and `SONIC_DPKG_CACHE_METHOD_OVERRIDE=none` must occur exactly once and remain
unchanged. Missing, duplicated, malformed, or changed options reject. The old
directories and metadata remain untouched, and no prior Bazel result hits are
available to lose. New Bazel work may populate the fresh cache. The changed
derived version-cache path also changes generated
`src/sonic-build-hooks/buildinfo/config/buildinfo.config`; receipts record that
file's before/after hashes and the final source audit captures the native
generated change. Version caching retains its recorded disabled setting.

## One native continuation

`native_recovery=resume-native` accepts only reviewed DASH owner commit
`c11a83d7d56726d622a1e58594f34b13092e130d`, directly following
`2a6e390b96a4fc17c191fa0da4b7ed1f40aed069`. The driver verifies the exact rename
`bazel/python_test.py` to `bazel/python_test_runner.py`, unchanged launcher
bytes/modes, and the three BUILD references. It records before/after hashes and
backs up the original overlay files in new diagnostic evidence. This working
tree overlay leaves the original Git HEADs, root merge identity, epoch, and all
other pins in place.

Admission acquires an exclusive lock on the existing build-directory inode,
checks all six pinned prior evidence hashes, checks the immutable worker and
configuration, and validates the complete original source graph and captured
source status/metadata. The prior source audit contains no historical hashes
for modified files; the diagnostic records current hashes and explicitly retains
that limitation. It rejects any existing diagnostic operation rather than
silently adopting or retrying it.

The preserved `fsroot.docker.trixie` is excluded from source-status traversal
only after proving it contains no tracked entry or gitlink in the captured
commit, current HEAD, or current index. Its original audit contains 77,936
untracked Docker-cache entries, including special modes which the original
checkout ownership restoration could clear. The new diagnostic records the
excluded metadata count and this limitation, while retaining the original
audit/hash unchanged. Other source checks, including similarly named siblings,
remain active. No validation of those cache contents is claimed.

The driver reuses the original `target` outputs and outer private
`state/native-9b664952f22b4205/docker-data`. It repeats only the original logged
`bazel-vs-native-inputs` Make command, with its original epoch/options, and skips
`init` and `configure`. It supplies a new private inner `DOCKER_ROOT` under
`state/native-diagnostic/inner-docker-data`: `Makefile.work` clears its selected
inner Docker root while parsing, so selecting the old root would destroy
retained state. The old inner Docker directory is preserved.

The fresh worker creates the original `sonicnative` account at `1001:1003`,
matching the inspected checkout owner. This is an explicitly recorded change
from the original worker's `1000:1000` execution identity. The Make argv,
account name, epoch, immutable image, and options stay the same apart from the
declared fresh cache source and inner `DOCKER_ROOT`. `Makefile.work` creates its slave
account using the invoking `id -u`/`id -g`; numeric IDs do not change the cached
base/slave image tags. The root-owned private daemon accesses its existing
Docker data and grants the new account socket access through its group.

No retained ownership transition or restoration is needed on success, failure,
repeated cancellation, or parent kill. The original cache directories, Docker
trees, checkout and prior outputs are never chowned by the diagnostic. The
pinned Docker preflight still assigns UID/GID `1000:1000` to its newly created
scratch only. New native outputs use the diagnostic account's `1001:1003`.

The owned worker mounts only this build root and receives no host Docker
socket. It retains the existing private cgroup and real Docker/BuildKit
preflight. Native Make can transitively run the existing audited P4RT TAR bridge;
this path does not enter the normal Bazel package or installer phases.

## Cancellation and evidence

The parent stops and removes only its own worker after checking its label,
immutable image, private cgroup namespace, and exact single bind mount. Every
host Docker client uses the same sanitized environment and explicit local
socket. An independent `always()` cleanup step covers a parent killed during
GitHub's short cancellation window. Before Docker creation, the driver
atomically reserves the invocation in this job's external evidence. Cleanup
requires matching repository, workflow, SHA, run, attempt, job, manifest, and
retained plan; a rejected second attempt never adopts the first one's worker.
Its Docker operations are bounded within the cleanup step's three-minute limit.
No global Docker cleanup runs.

New receipts are `diagnostic-running`, `diagnostic-passed`, `diagnostic-failed`,
or `diagnostic-rejected`, with `clean_checkout_proof=false` for a reserved
continuation. Original failed receipts and logs remain unchanged. A final source
audit records the overlay and any subsequent native source transformations,
including the existing FRR transformation when reached. Normal native
verification rejects these diagnostic statuses.

Even a successful continuation is incremental diagnostic evidence for the old
merge plus the recorded overlay. It is not a clean build of the new owner
gitlink or current PR2 head. Final clean-checkout package, installer, verification,
and artifact proof remain separate work.

## Local validation

The hosted workflow checks out four support modules from exact PR2 `2546efcc`.
Runtime verifies and imports `build_area.py`, `native_build.py`, and
`source_workspace.py` by explicit file path. The tests additionally verify and
import `image_inputs.py` to prove that normal verification rejects a diagnostic
receipt. Script-directory modules and `PYTHONPATH` cannot select other helper
revisions.

Run the stdlib Git/filesystem tests with `SONIC_RECOVERY_SUPPORT` pointing to
that checkout's `tools/bazel/ci`, then:

```sh
python3 -B -m unittest discover -s tools/ci/native_recovery -p '*_test.py' -v
```

The separate local Docker fixture uses a tiny Make producer with real worker
creation, private dockerd/cgroups/BuildKit preflight, and owned cleanup. Its
filesystem admission is substituted because the local fixture is not the
ThinkStation allocation. It proves lifecycle, isolation, retained-marker and
receipt preservation, and cancellation behavior, not a production package build
or a cache hit.
