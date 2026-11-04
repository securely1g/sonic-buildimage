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

## Reviewed sFlow source validation

The optional `sflow-hosted` manual mode in `bazel-oci.yml` requires paired full
source and registry commit SHAs. It selects AMD64 for the existing archive and
combined SWSS/sFlow installed job. Both check out the same source; the loaded
job verifies the native selection before loading the sFlow images and retains
the original SWSS source/hash checks. Blank/default mode retains the
existing owner graph and architecture matrix.

The reviewed mode preserves setup-bazel caches, adds a checksum-verified shared
downloader configuration, and reclaims only the three unused SDK mounts on a
disposable GitHub runner. It retains all original sFlow exports, source-owner
tests, static and incremental checks, plus the private Redis/veth, mapper,
polling and UDP exporter gates. It skips unrelated general checks and VS jobs. The exact owned review branch excludes the existing
DEB-generating kernel fixtures; remove that temporary exception before Ready.
This workflow does not establish independent clean-root reproducibility or final
VS installer acceptance.
