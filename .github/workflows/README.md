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


## Reviewed Router Advertiser source on hosted AMD64

Run `bazel-oci.yml` with `source_image=radv` and paired `source_revision` and
`registry_revision` values to validate an immutable source and registry snapshot.
Both revisions must be 40-character commit IDs. The Radv jobs use hosted AMD64
for this selection; normal pull requests and `source_image=none` retain the
self-hosted native runner, workflow checkout, registry, cache and mirror behavior.
The native and loaded jobs use the same selected source. Loaded validation
checks the retained source/catalog/controller receipt and loaded-driver hash
before the private Redis, IPv6 router-advertisement and VLAN-readiness checks.

The selected source must provide `lldp_draft_cache.py`, executable
`review_bazel.sh` and its `review_bazel_rc.py` companion, `prepare_pigz.py`, and
the Radv build, loaded and Make-handoff entrypoints in their existing
`tools/bazel` locations. The workflow checks these entrypoints before building;
a source snapshot without this interface is not supported by this mode.

The reviewed selection retains the source/TAR action audit, static symbols and
configuration checks, template-edit restoration, generated lock/graph and public
artifacts. It uses the reviewed cache, checksum-pinned transport and disposable
hosted-runner space step without the self-hosted private-cache settings. Other
source-image worker choices and normal regression/installer checks are unchanged.
This is component image validation, not installer acceptance.
