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

## Reviewed Eventd source on AMD64

In `bazel-oci.yml`, select `source_image=eventd` and supply paired immutable
40-character `source_revision` and `registry_revision` commits. The catalog
input applies only to Eventd; other source-image selections keep their existing
interfaces. Leave `source_inputs_run` blank for Eventd.

The manual Eventd jobs use distinct check and artifact names. They check out the
selected source under `review-source` for its Eventd drivers. The controller
checkout retains checkout identity, the archive runner and cache namespace,
startup repair, mirrors, upload policy and public sanitization. Its review
launcher keeps selected checked-in build defaults, replaces the local downloader
default with the controller's verified mirrors, ignores the selected user RC,
and selects the immutable SONiC catalog plus BCR. Normal archive, installed
Eventd runtime and other-image jobs keep their existing execution paths.

The exact documented source/catalog pair retains the size- and SHA256-verified
libpcre2 prefetch. Only its controller-validated diagnostic receipt is public.
`reviewed-selection.json` binds source, catalog, controller checkout identity
and the loaded-driver hash before installed Redis and event-delivery checks.
