# Build event services with Bazel

Bazel builds the same four C++ programs as this component's Makefile: `eventd`,
`eventdb`, `events_tool` and `rsyslog_plugin`. It links the source-built public
SWSS Common library and the declared Debian dependencies. Source lists remain
in this component's `BUILD.bazel`; CI compares them with the Make source lists.

## Runtime and debug archives

From the sonic-buildimage repository root:

```sh
bazel build \
  //src/sonic-eventd:eventd_runtime \
  //src/sonic-eventd:eventd_runtime.debug_symbols \
  //src/sonic-eventd:rsyslog_plugin_runtime \
  //src/sonic-eventd:rsyslog_plugin_runtime.debug_symbols
```

The two runtime TARs match `debian/sonic-eventd.install` and
`debian/sonic-rsyslog-plugin.install`: four stripped executables, two unchanged
Python utilities and three unchanged configuration files at their existing
installed paths. The corresponding debug TARs contain matching build-ID symbol
files under `/usr/lib/debug/.build-id`. The shared `sonic_deploy_tar` rule forces
a debug-enabled producer build before splitting each binary, so an optimized
caller still receives usable symbols. These targets produce no DEB package.

The [Syncd source-image work](https://github.com/securely1g/sonic-buildimage/pull/13)
is the consuming image integration. It should use both runtime targets in its
base layer and both debug targets in its debug layer. Image composition and
container/guest validation are separate changes.

## Native tests

```sh
python3 src/sonic-eventd/bazel/ci.py \
  --architecture amd64 --artifacts /absolute/path/eventd-validation
```

Use `--architecture arm64` on a native ARM64 Debian Trixie host. The helper selects
matching execution and target platforms. CI runs these two native configurations.

During review, the default registry remains on maintained `main`. ARM64 Lua
headers require [the landed shared importer fix](https://github.com/securely1g/sonic-build-infra/pull/33).
The workflow prepares upstream Distroless 0.9.4 plus the existing include-fragment
and whitespace fixes and the exact owned multiarch patch from maintained source
`0b76749e7297476c5bfde0f0b3572ddf96052d01`. Reproduce those pinned
preparation steps in `.github/workflows/bazel-eventd.yml`, then add
`--bazel-arg=--override_module=rules_distroless=/absolute/path/distroless-eventd`
to the helper (or `--override_module=...` to direct Bazel commands). The eventual
[`0.9.4.sonic.3` registry publication](https://github.com/securely1g/sonic-bazel-registry/pull/59) replaces this temporary source override.
A bare ARM64 build with the current default `0.9.4.sonic.1` retains the importer
bug; CI explicitly validates the fixed source candidate.

Before building, the helper audits the action graph and rejects DEB production
or Make/package-build wrappers. It runs all three original C++ suites: six
eventd tests, four eventdb tests and thirteen rsyslog-plugin tests. Assertions
and production behavior remain unchanged. Skipped cases, a missing Redis service
or the original tests' Redis-unavailable fallback fail the wrapper.

Test JSON files are copied into a temporary working directory before replacing
Redis endpoints. Each configured Redis instance runs from the declared Debian
test runtime with a unique loopback port and private Unix socket, without
persistence. Cleanup terminates only those child processes. The suites are marked
exclusive because the original eventd tests use fixed local ZMQ ports. No host
Redis service or production database is used.

The helper also checks all installed paths and modes, script/configuration
bytes, native ELF architecture, position-independent executables, stripped
runtime files, DWARF symbols, build IDs and GNU debuglink checksums. It retains
both runtime/debug TAR pairs, package hashes, test events and source-coverage
evidence. Tests retain their original GoogleTest XML and service fixture counts
in Bazel's undeclared test outputs.

Run the helper after adding or moving a source file or installed payload. This
validates the component's build and packaging; it does not prove complete SONiC
image integration, guest boot or forwarding.
