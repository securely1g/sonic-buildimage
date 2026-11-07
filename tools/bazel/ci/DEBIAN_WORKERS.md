# Debian Trixie CI workers

The workflows that previously selected `ubuntu-24.04` or `ubuntu-24.04-arm`
use `[self-hosted, Linux, X64, debian-trixie-container]`.
The runner pool is managed by the existing K3s slot controller in
[agi-projects](https://github.com/securely1g/agi-projects/tree/main/sonic-slave-wrk-mgr/k3s).

| Part | Worker contract |
| --- | --- |
| Actions listener, checkout, artifact upload/download | Native AMD64 |
| AMD64 build and test steps | Debian Trixie, native AMD64 |
| ARM64 build and test steps | Debian Trixie ARM64 tools container under QEMU |
| Shell | `sonic-ci-shell {0}`, selected by `SONIC_CI_ARCH` |
| Bazel | Preinstalled 8.5.1 with the worker's verified cache compatibility support |
| Docker | A private daemon and data directory for each disposable runner |
| Resources | Two CPUs, 6 GiB Pod memory, and a 32 GiB Bazel cache within the 80 GiB Pod storage limit |
| Files and ownership | Workspace, cache, home, and GitHub output files shared at matching paths with UID/GID 1000 |
| Tools | Git, sudo, Python, compiler tools, archive tools, Node.js, GitHub CLI, and Docker CLI |
| Credentials and cache endpoint | Configured by the operator outside workflow YAML |

ARM64 is emulated on the AMD64 host. The ARM64 steps execute ARM64 Bazel,
compilers, Python, tests, and installed image programs; their existing machine
checks, ELF checks, dependency locks, and test targets remain in effect. The shell
reports its execution mode in the job log and preserves the step's exit status.
Actions that transfer source or artifacts continue to run on the native listener.
The ARM64 profile uses Bazel's process-wrapper sandbox and restart-based
repository fetching, which are verified with QEMU; the AMD64 profile retains its
ordinary Linux sandbox and repository behavior.
YANG and Eventd allow 90 minutes on ARM64 for emulated dependency analysis and
compilation; their native AMD64 limits remain 30 and 45 minutes respectively.
The archive job allows six hours on ARM64 because it also repeats builds across
fresh workspaces to verify cache reuse; its AMD64 limit remains three hours.
Module evidence collection shuts down the completed build's Bazel server before
inspecting extensions, releasing analysis memory while preserving cached outputs
and the tested module lock.

The image supplies Bazel directly, so `setup-bazel` must not replace its launcher
or add a second cache configuration. Workflow package installations use sudo
because runner processes use UID 1000. A job must not remove host SDK paths or
prune another job's Docker store. Finishing a job removes its Pod and private
Docker/workspace volumes; the controller supplies a new ephemeral runner.

Source downloads retain their pinned versions and checksums. P4 archives use the
registry's existing `p4-source-inputs-20261021` release, with the original OpenSUSE
endpoint as a fallback. The three small upstream `.dsc` files are retained in
`src/p4lang/source-descriptors/`. The transport helper checks all nine inputs
against the unchanged manifest and records which source supplied each file.
