#  Github actions README
This is an introduction about auto-cherry-pick workflow.
take 202205 branch for example:
1. pr_cherrypick_prestep:
```mermaid
graph
Start(Origin PR) --> A{merged?}
A -- NO --> STOP
A -- YES --> A1{Approved<br> for 202205<br> Branch?}
A1 -- NO --> STOP
A1 -- YES --> A2(pr_cherrypick_prestep)
B(pr_cherrypick_prestep)
B --> B1{cherry pick<br>conflict?}
B1 -- YES --> B2(Add tag:<br>Cherry Pick Confclit_202205) --> B3(Add comment:<br>refer author code conflict) --> STOP1(STOP)
B1 -- NO --> B4(Create New PR) -- success --> B5(New PR add tag:<br> automerge) --> B6(New PR add comment:<br>Origin PR link) --> B7(Origin PR add tag:<br>Created PR to 202205 Branch) --> B8(Origin PR add comment:<br>New PR link)
B4 -- fail --> STOP1
```

2. automerge:
```mermaid
graph
Start(PR azp finished successfully) --> A{author:<br>mssonicbld?}
A -- NO --> STOP
A -- YES --> B{tag:<br>automerge?} -- YES --> C(Merge PR)
B -- NO --> STOP
```

3. pr_cherrypick_poststep:
```mermaid
graph
A(PR is Merged) --> B{tag:<br>automerge?}
B -- YES --> B1{author:<br>mssonicbld?}
B1 -- YES --> B2{"title starts:<br>[action] [PR:123]"}
B2 -- YES --> C(Origin PR remove tag:<br> Created PR to 202205 Branch) --> D(Origin PR add tag:<br> Included in 202205 Branch)
B -- NO --> STOP
B1 -- NO --> STOP
B2 -- NO --> STOP
```


## Unprivileged VS installer assembly

The self-hosted runner does not need host `sudo`. A short setup container uses
the exact worker image, individual mounts for the job's state/evidence paths,
and only `CHOWN`/`FOWNER` capabilities to prepare shared filesystem permissions.
It does not run Make or Bazel. The final build container then runs with all
capabilities dropped and no-new-privileges enabled. CI also disables Bazel's
workspace lockfile writes and convenience symlinks; the hosted source-download
job retains generated resolution evidence.

The hosted job fetches and verifies the five GitLab source archives already used
by the OCI workflow. Native Make receives their verified distdirs, and the same
source bytes travel in the final-assembly handoff. Both assembly workers recheck
the BCR SHA512 values before using them, so a cold build does not depend on an
existing runner repository cache or direct GitLab access.

`bazel-vs-unprivileged.yml` validates the optional containerd-backed
`target/sonic-vs.bin` assembly path on AMD64. It preserves a job boundary between
native source production and final assembly:

1. A hosted check creates a content-only containerd store as UID/GID 1000 with
   all capabilities dropped, no new privileges, default seccomp and no Docker
   socket. A separate privileged container checks that the exact output still
   supports image unpacking and runtime filesystem behavior.
2. `Native Make prerequisites (AMD64)` uses the existing source-build runner
   pool to run `bazel-vs-native-inputs`. It finalizes the host filesystem and
   exports the host receipt, service archives and their producer identities.
   Its assembly worker derives from the actual Trixie `sonic-slave` image;
   immutable image IDs and layer ancestry are recorded. This job retains
   privileged execution and does not produce the final installer.
3. `Unprivileged sonic-vs.bin assembly (AMD64)` downloads the same run's
   checksummed handoff into an independent checkout, imports that exact worker
   image, and runs the ordinary Make target **inside** its unprivileged
   `sonic-slave`-based container. The container runs as UID/GID 1000 with all
   capabilities dropped and no Docker socket. A fresh Bazel output root avoids
   reusing privileged final-assembly actions. CI records both Docker settings
   and the process's effective capabilities, seccomp and no-new-privileges state.
   It checks the full ONIE/ZIP byte chain before uploading `sonic-vs.bin`.

Native prerequisites and final assembly use separate GitHub jobs and exchange
an explicit artifact, so they do not depend on scheduling onto the same machine.
Both jobs use the shared `sonic-vs-bin` label for same-repository PR, push, and
manual runs. This bin-only pool is separate from the full-image pool that
requires KVM. The native preflight selects `sonic-vs.bin`,
which does not require KVM because this workflow does not boot the installer.
The transfer includes the worker image and service archives and can be large;
its time is not a final-assembly speed measurement. The runtime test uses small
storage fixtures, while the full job uses native source-produced SONiC inputs.
Neither establishes guest boot or forwarding coverage.
