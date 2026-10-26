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

`:check_programs` builds the upstream check binaries. `:upstream_check` executes
the upstream check suite, including its Valgrind memory checks. The known
Valgrind/tcmalloc incompatibility remains documented in the FRR contribution
notes; this installation-output change preserves tcmalloc and the upstream test
behavior.
