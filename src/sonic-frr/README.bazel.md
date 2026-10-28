# Native FRR build and OCI installation archives

The pinned FRR source and SONiC patches are compiled with native Bazel C/C++
rules. This implementation exports installation TARs rather than final Debian
packages. Existing Make package builds retain their upstream implementation.

Public installation targets:

| Target | Contents |
| --- | --- |
| `:frr_layout` | Routing daemons, modules, shared libraries, configuration, service resources and runtime documentation |
| `:frr_elf.debug_symbols` | Debug symbols matching the deployed FRR ELF files |
| `:frr_snmp_layout` | FRR AgentX shared library and associated resources |
| `:frr_snmp_elf.debug_symbols` | Debug symbols matching the AgentX library |
| `:pythontools_layout` | FRR Python administration tools |

Use `--platforms=@sonic_build_infra//platforms:x86_64_trixie` or the corresponding
`aarch64_trixie`, `x86_64_bookworm`, or `aarch64_bookworm` platform. Execution tools
use the selected host platform. `--//src/sonic-frr:tcmalloc` and
`--//src/sonic-frr:snmp` preserve the component build options; both default to true.
The image consumer selects applicable TARs and supplies required system packages,
accounts, writable directories, service startup and cache generation. The TARs
can be layered directly; no intermediate DEB assembly or maintainer-script
execution is provided by this component build.

`:check_programs` builds the upstream check binaries. `:upstream_native_check`
runs the ordinary upstream checks with the default tcmalloc configuration.
`:upstream_check` retains the complete suite, including all six Valgrind checks;
run it with `--//src/sonic-frr:tcmalloc=false` so Valgrind owns allocation
instrumentation. Both suites support native AMD64 and ARM64 execution.
CI keeps the production TARs and ordinary tests on the default allocator and
runs the complete memory-check suite separately for Bookworm and Trixie on
both architectures. Cross-compiling the check programs does not execute them.
This avoids the observed tcmalloc initialization errors under Bookworm Valgrind
and preserves strict memory-error exit codes without suppressions.

## Native source CI

The `Bazel FRR and sonic-fib` workflow builds the installation TARs, matching
symbols and upstream check programs on native AMD64 and ARM64. FRR metadata
is selected on its declared Trixie platforms; sonic-fib metadata covers both
distributions. Each
architecture tests both the Trixie and Bookworm target platform using the
declared runtime. It runs the original FRR checks, including Valgrind, and both
sonic-fib tests. A failing upstream check remains a failing job.

While registry PRs #146–#150 are unmerged, the workflow uses the single frozen
candidate registry `88b921106d3c5097204752a38be6c6b29bad2399`. Its
`DRAFT_VALIDATION_INPUTS.json` records every composed prerequisite. The candidate
selects infrastructure `0.0.25-917c7d5792f88c9de485ea5172a835bb0c06bf6a`,
Distroless `.sonic.13`, libnl3 `.sonic.7`, libyang `.sonic.6` and Net-SNMP
`5.9.3.sonic.1`. The default `.bazelrc` still uses maintained `main`.

For local candidate validation, use the command and explicit platform flags in
`.github/workflows/bazel-frr.yml`: its startup flags load `common.bazelrc`
without also loading the default registry endpoint. Do not append the candidate
to the default endpoint as a fallback. Replace the temporary CI endpoint with
canonical `main` and verify landed source revisions before review readiness.
The workflow retains checkout inputs, resolution lock, build/test events and
named output files with SHA-256 checksums. These component results do not
establish a complete SONiC image or the inherited image workflows' success.
