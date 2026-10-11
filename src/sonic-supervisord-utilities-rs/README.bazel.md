# Rust supervisor listener with Bazel

The public `:runtime` and `:runtime.debug_symbols` targets install the
source-built listener at `/usr/bin/supervisor-proc-exit-listener-rs` and its
matching detached build-ID symbols. This matches `debian/install`. Both native
AMD64 and ARM64 Debian Trixie are supported; Bazel never produces a DEB here.

`:supervisor_library` owns this component's Rust code. It depends directly on
`@sonic_swss_common//crates/swss-common:swss_common`; Common retains its source,
C bindings and native library. All application Rust dependencies use the single
`sonic-rust-deps` graph. The buildimage root also selects landed SWSS commit
`252a506ae5a94e36a4e330b736a3781a9f7bcf9b` and registered Common 0.0.2, which
already use that graph. No local Common source checkout is needed by Bazel.

```sh
bazel build --config=release //src/sonic-supervisord-utilities-rs:runtime \
  //src/sonic-supervisord-utilities-rs:runtime.debug_symbols
bazel test //src/sonic-supervisord-utilities-rs:unit_tests \
  //src/sonic-supervisord-utilities-rs:integration_tests \
  //src/sonic-supervisord-utilities-rs:listener_tests \
  //src/sonic-supervisord-utilities-rs:package_test
```

The original Rust tests run serially because they replace process-wide
functions and environment values. The package test executes the installed,
stripped program's `--help` using its declared runtime libraries and checks the
install path, ownership/mode, native ELF architecture, Common SONAME, build ID,
DWARF/debuglink CRC and GDB source-line lookup. It needs native binutils and GDB.
The shared dependency tests additionally exercise pipe readiness, signal-zero
probing, the pinned test injector and syslog initialization on both CPUs.

The standard `--config=release` selects Bazel's optimized Rust/C++ compilation
mode; debug information is retained before the shared packaging rules split
it. The standard C++ linker integration currently supplies Rust's unwinding
panic runtime. Cargo's release abort-on-panic/LTO profile is therefore not
replicated, and this migration does not claim byte-for-byte release identity. Final container service and watchdog behavior are image-level
checks beyond the installed CLI and original test suites.

## Cargo and Bazel locks

`Cargo.toml` and `Cargo.lock` remain unchanged for the native Make/Cargo path.
Bazel's authoritative third-party lock is the shared `sonic-rust-deps` module.
The following direct dependencies differ between those two existing selections;
all Bazel versions satisfy the unchanged component manifest's version ranges.
Required features are retained and combined with the other shared consumers.

| Dependency | Native Cargo lock | Shared Bazel lock | Manifest range |
| --- | --- | --- | --- |
| clap | 4.5.42 | 4.5.53 | 4.0 |
| log | 0.4.27 | 0.4.28 | 0.4 |
| tracing-subscriber | 0.3.19 | 0.3.20 | 0.3 |
| serde_json | 1.0.141 | 1.0.145 | 1.0 |
| tempfile | 3.20.0 | 3.23.0 | 3.0 |

Mio 0.8.11, nix 0.27.1, syslog-tracing 0.3.1, thiserror 1.0.69,
tracing 0.1.41, tracing-log 0.2.0 and scopeguard 1.2.0 match both locks.
The test injector remains pinned to upstream Git commit
`c7317906a1d514ef99bf2fbd4c943908c265d280` (injectorpp 0.4.0).
The shared graph's established release logging cap remains in effect.

## Draft dependency

The committed registry URLs stay on SONiC registry `main` and BCR. Until the
new shared aliases are registered from merged source, validation explicitly
supplies `--override_module=sonic-rust-deps=/path/to/sonic-build-infra/rust/deps`
from the reviewed shared-dependency source candidate. The workflow pins that
source commit. Remove the temporary override and select the registered module
version before declaring this consumer ready.
