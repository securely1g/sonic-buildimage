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
runs the complete memory-check suite separately on native AMD64/Trixie. Cross-compiling the check programs does not execute them.
This avoids the observed tcmalloc initialization errors under Bookworm Valgrind
and preserves strict memory-error exit codes without suppressions.

## Native source CI

The `Bazel FRR and sonic-fib` workflow builds the installation TARs, matching
symbols and upstream check programs on native AMD64/Trixie. This matches the
current migration scope; the retained ARM64 and Bookworm target definitions are
not current CI coverage. It runs the original FRR checks, including Valgrind,
both sonic-fib tests, and the installed SDK/runtime and schema-invalidation
checks documented in `../libraries/sonic-fib/README.bazel.md`. A failing upstream
check remains a failing job. The full VS installer is outside this source-only
PR's no-DEB scope and its skipped job is not image validation.

`verify_runtime.py` checks the deployed FRR and SNMP ELF modes, architecture,
build IDs, debug-link CRCs and matching DWARF. It preserves the owner debug
documentation symlinks and verifies isolated BGPD source lookup succeeds only
with the matching symbol TAR. The receipt records actual SONAME/DT_NEEDED
entries. In the selected source, the SONiC FPM module includes libFIB's public
header but makes no libFIB API call, so the linker drops its unused shared
library. The existing Make build and runtime metadata declarations are retained;
a recipe dependency alone is not proof of an ELF loader dependency.

Draft validation uses the same frozen candidate registry in the workflow and
`tools/bazel/ci/frr_draft_bazel.sh`. The catalog starts from maintained `main` at
`a802726af663bce3890d729f0f88b9c939cdb4eb`, preserves every published entry, and
adds only FIPS `.5` from registry PR #221 and Net-SNMP `5.9.3.sonic.1` from #150.
The Net-SNMP entry supports the retained Bookworm declaration; AMD64/Trixie
continues to use the locked distribution package.

Native builds select published infrastructure `.25`, Common `.9`, libnl3
`.sonic.7`, Distroless `.sonic.16` and libyang `.sonic.6`. Common and FIPS now
select their landed source revisions. The infrastructure override retains the
accepted TAR deduplication and compiler system-header handling, which the
published `.26-51c838da` recipe drops. Its direct APIs used by FRR, Sysmgr and Common
match `.26`; configured and native checks still validate the combined selection.
Exact versions are declared in `MODULE.bazel`. FRR/libFIB sources, SONiC patches
and tests are unchanged. The default `.bazelrc` still uses maintained `main`;
the two candidate entries must land and current CI must pass before this PR is
ready for review.

For local candidate validation, use the command and explicit platform flags in
`.github/workflows/bazel-frr.yml`: its startup flags load `common.bazelrc`
without also loading the default registry endpoint. Do not append the candidate
to the default endpoint as a fallback. Replace the temporary CI endpoint with
canonical `main` and verify landed source revisions before review readiness.
The workflow retains checkout inputs, resolution lock, build/test events and
named output files with SHA-256 checksums. These component results do not
establish a complete SONiC image or the inherited image workflows' success.
