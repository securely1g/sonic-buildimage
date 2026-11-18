# Check installed runtime and debug images

`tools/bazel/ci/validate_runtime.py` is the shared Docker validation entry point
for base, SWSS and Syncd images. It does not build an image or package. Static
OCI validation and Docker loading remain distinct: the former checks the
layers and export, while the latter checks the filesystem Docker actually
installs from that export.

## Check files and symbols in CI's exported images

The base workflow runs this mode on native AMD64 and ARM64. The full VS workflow
runs it on native AMD64 for config-engine, SWSS and Syncd runtime/debug images:

```sh
python3 -B tools/bazel/ci/validate_runtime.py \
  --images-policy artifacts/vs/swss-installed-images-policy.json \
  --images-policy artifacts/vs/syncd-installed-images-policy.json \
  --target target \
  --output artifacts/vs/installed-images.json
```

Bazel generates the policy from the image owners' declarations and actual OCI
outputs (`//tools/bazel/tests:swss_installed_images_policy` with the normal
SWSS flags and `//tools/bazel/tests:syncd_installed_images_policy` with
`--config=syncd-dash`). These match Make's two build configurations. The runner
combines identical shared-base declarations and rejects conflicting identities.
The base-image driver uses the same policy generation for its two images.
The policy identifies the archives, expected image configuration, file metadata, parent relationships,
directory aliases and runtime/debug pairs. The validator uses ordinary
`docker load`, then probes immutable image IDs in isolated containers.

For example, config-engine supplies
`usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0` and
`run/redis/sonic-db/database_config.json`. SWSS and Syncd must retain those
files' hashes, ownership and modes, and `/var/run` must remain a symlink to
`/run`. The database configuration must be root-owned `0644`. A child layer
that overwrites the library or replaces the symlink causes validation to fail.
Common's deployed ELF and its debug file are copied from the containers;
matching build IDs, DWARF and debuglink CRC are checked by the shared ELF helper.
These file probes do not start the SONiC services.

This mode expects a private Docker daemon or an ephemeral runner dedicated to
this job: loading the archives retains their published tags. Full VS CI
uses the direct SONiC slave worker's private Docker daemon for loading and
probing. The retained legacy job prepares a separate nested daemon and cgroups.
The native base workflow retains
its additional installed-program, Python/YANG and FIPS smoke checks.

## Build and validate the source-based Syncd images

The Syncd image graph from
[PR #13](https://github.com/securely1g/sonic-buildimage/pull/13) and the generic
archive/runtime checks are already on `master`. This entry point adds the
Syncd-specific smoke policy and builds the declared source-image inputs before
checking them together. The standalone validator regression workflow does not
build that graph. After preparing its source dependencies and Make manifests as
described in the image's `platform/vs/docker-syncd-vs/bazel/README.md`, run this
command in the native AMD64 build environment with Docker available:

```sh
python3 tools/bazel/ci/validate_syncd_image.py \
  --output artifacts/syncd-installed \
  --redis-server /usr/bin/redis-server
```

It audits the selected Bazel actions, builds both image archives, and validates
their installed packages and source symbols, including the inherited base.
The action audit rejects Debian package production. It then runs the isolated
container checks below. Reports in the output directory bind each check to the
image, archive and package inputs it examined. The installed package database
is checked against generated status and metadata outputs. The `_ssh` account
and `ssh-agent` ownership/mode remain checked; no checked-in package-state
snapshot is required. The declared installation-links TAR is checked against
both final images, including resolution of every link destination.

## Run against prepared archives

First run the image's static validation to obtain `validation.json`, including
its archive hashes and native ELF inventory. For the Syncd P4 image that report
comes from `//tools/bazel/tests:syncd_p4_images_test` on its image branch. Keep the
report and both exported archives together. Then, on a native AMD64 Linux host
with Docker:

```sh
python3 tools/bazel/ci/validate_runtime.py \
  --runtime /absolute/path/docker-syncd-vs.gz \
  --debug /absolute/path/docker-syncd-vs-dbg.gz \
  --static-report /absolute/path/validation.json \
  --policy platform/vs/docker-syncd-vs/config/runtime_smoke.json \
  --redis-server /usr/bin/redis-server \
  --output /absolute/path/runtime-validation.json
```

`--redis-server` is an existing native Redis executable used **only as a test
helper**. Its shared-library requirements must be available in the tested
image. The validator copies the helper into a private directory, records its
SHA256 and in-container version, and mounts it read-only. It starts Redis only
inside the test container, with TCP disabled and persistence disabled. Syncd
initializes its database-backed logger before processing `--help`, so a working
Unix socket is required even for that command. The helper is not added to the
image or counted as a migrated runtime component.

The policy lists the installed commands, Python imports, shared-library groups,
startup configuration and P4 test to exercise. Shared-library groups include
providers used by the real consumer; for example, DASH SAI uses Syncd's
Protobuf/PI providers. Loading plugins alone can incorrectly report missing
symbols that their consumer supplies.

## What is checked

- Both archive hashes/sizes match the static report; the loaded image has the
  expected execution configuration, native architecture and filesystem layers.
- Every recorded ELF resolves its dynamic dependencies inside the actual image.
  Selected public libraries and plugin groups also load with immediate symbol
  resolution. Installed commands run with `LD_BIND_NOW=1`.
- SONiC Python packages import. The installed startup script copies a sample SAI
  profile correctly, and every installed supervisor program resolves to an
  executable. This does not launch the full supervisor stack.
- The installed P4 compiler compiles a tiny drop program. The installed BMv2/PI
  process starts a gRPC listener on container loopback with that program. No
  packets or external service connections are sent.
- For each available runtime/debug pair, GDB finds a function's source filename,
  line and argument types from the installed debug layer. A copy of the stripped
  binary without accessible symbols must fail the same lookup. Source text is
  not supplied, and GDB does not run or attach to a process.

The JSON report records archive/configuration/probe/policy hashes, native
machine, helper provenance, command output, symbol lookups, preserved symbol
gaps and failures. The command exits nonzero if any selected check fails.

## Isolation and limits

Each image is loaded under a unique temporary tag. The test container has no
network access, Linux capabilities or Docker socket; its root filesystem is
read-only. Temporary directories and the two startup fixture paths are writable.
A fresh anonymous `/run/redis` volume preserves the image's shipped database
configuration and holds the test socket; Docker removes it with the container.
Cleanup touches only this invocation's unique container and tag. No host service
is started, stopped or reconfigured.

These checks establish installed-container behavior for the recorded bytes.
They do not establish guest boot, SAI switch initialization, forwarding,
package-manager equivalence, FIPS certification or complete debug coverage.
The Syncd package policy requires matching source symbols for PI, BMv2, P4C
and DASH SAI as well as its other native source packages. Runtime probes alone
do not prove source provenance; the source receipts and action audit establish
that separate property.

The `Installed-image validator` workflow tests both modes on native AMD64 and
ARM64, including compiling a tiny C library, checking split symbols with GDB,
rejecting a corrupted debug file, and checking the workflow isolation guards. It does not build or run a SONiC image, compile P4/DASH producers, or create
DEB packages. The current Syncd image policy is AMD64-only;
ARM64 validator CI must not be reported as ARM64 image support.
