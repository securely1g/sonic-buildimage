# Debian Trixie CI workers

Regular CI jobs that previously selected `ubuntu-24.04` or `ubuntu-24.04-arm`
use `[self-hosted, Linux, X64, debian-trixie-container]`. The manually dispatched
Make cache proof retains its existing native hosted runners while its Linux
namespace and host AppArmor requirements are resolved. Its PR regression tests
use the Debian pool.
The runner pool is managed by the existing K3s slot controller in
[agi-projects](https://github.com/securely1g/agi-projects/tree/main/sonic-slave-wrk-mgr/k3s).

| Part | Worker contract |
| --- | --- |
| Actions listener, checkout, artifact upload/download | Native AMD64 |
| AMD64 build and test steps | Debian Trixie, native AMD64 |
| ARM64 build and test steps | Debian Trixie ARM64 tools container under QEMU |
| Shell | `sonic-ci-shell {0}`, selected by `SONIC_CI_ARCH` |
| Bazel | Native AMD64 8.5.1 coordinator for both target architectures, with the worker's verified cache compatibility support |
| Docker | A private daemon and data directory for each disposable runner |
| Resources | Two CPUs, 8 GiB Pod memory, and a 32 GiB Bazel cache within the 80 GiB Pod storage limit; ARM tools are limited to 7 GiB to reserve memory for the runner |
| Files and ownership | Workspace, cache, home, and GitHub output files shared at matching paths with UID/GID 1000 |
| Tools | Git, sudo, Python, compiler tools, archive tools, Node.js, GitHub CLI, and Docker CLI |
| Credentials and cache endpoint | Configured by the operator outside workflow YAML |

ARM64 is emulated on the AMD64 host. The ARM64 steps execute ARM64 compilers,
Python, tests, and installed image programs; their existing machine
checks, ELF checks, dependency locks, and test targets remain in effect. The shell
reports its execution mode in the job log and preserves the step's exit status.
The Bazel coordinator runs natively, while the ARM64 target and execution
platforms retain ARM64 toolchains. The worker shares its private `/b` output base
with the ARM tools to keep generated compiler arguments within Linux's limit.
Actions that transfer source or artifacts continue to run on the native listener.
The ARM64 profile uses Bazel's process-wrapper sandbox and restart-based
repository fetching, which are verified with QEMU; the AMD64 profile retains its
ordinary Linux sandbox and repository behavior.
The private Redis fixtures in Eventd and the installed-image probes identify
QEMU's synthetic memory map before ignoring its false ARM kernel COW warning.
Persistence remains disabled and the evidence records the ignored warning.
Eventd still requires all 25 test cases, and native ARM retains the kernel check.
YANG allows 90 minutes and Eventd allows 180 minutes on ARM64 for emulated
dependency analysis and compilation; their native AMD64 limits remain 30 and
45 minutes respectively. Rust allows 240 minutes on ARM64 and 90 on AMD64;
the cold ARM64 canary was still compiling when its 180-minute limit expired.
The base-image build allows six hours on ARM64 and two on AMD64. Its cold ARM64
canary passed all seven tests but was still compiling source dependencies
when the previous four-hour limit expired.
Libteam and its SWSS consumers allow six hours on ARM64 and three on AMD64.
The archive job allows eight hours on ARM64 for the complete Python suites and
cache checks across fresh workspaces. Its AMD64 limit remains three hours and
also covers the subsequent Sysmgr source-image build and validation.
Its long-test budget is two hours on ARM64 so the complete 356-case config-engine
suite can execute under emulation. AMD64 retains the 15-minute long-test budget,
and the other Bazel timeout categories retain their defaults.
Module evidence collection shuts down the completed build's Bazel server, gives
the extension inspection a separate 4 GiB heap, and stops that server before
installed-image checks. Source builds retain the worker's 2 GiB heap; cached
outputs and the tested module lock are preserved. Inspection stderr is retained
with the other diagnostics.

The image supplies Bazel directly, so `setup-bazel` must not replace its launcher
or add a second cache configuration. When selecting an isolated registry, the
CI wrapper explicitly retains the worker's system RC and operator-managed home
RC. This preserves resource limits, ARM sandbox settings, and both local and
remote caches. Other callers retain the wrapper's original RC isolation.
Workflow package installations use sudo
because runner processes use UID 1000. A job must not remove host SDK paths or
prune another job's Docker store. Finishing a job removes its Pod and private
Docker/workspace volumes; the controller supplies a new ephemeral runner.

Source downloads retain their pinned versions and checksums. P4 archives use the
registry's existing `p4-source-inputs-20261021` release, with the original OpenSUSE
endpoint as a fallback. The three small upstream `.dsc` files are retained in
`src/p4lang/source-descriptors/`. The transport helper checks all nine inputs
against the unchanged manifest and records which source supplied each file.
