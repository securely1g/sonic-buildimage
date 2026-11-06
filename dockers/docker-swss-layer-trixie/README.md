# Shared SWSS image from source owners

The shared SWSS layer consumes the source owners' runtime and debug TARs. The
image repository keeps OCI assembly and installed-image checks; SWSS and libteam
own their compilation, native dependencies and exported artifacts. Existing Make
recipes, source submodules and patch series remain unchanged.

| Component | Selected source owner | Image input |
| --- | --- | --- |
| SWSS | `sonic-swss@0.0.2-f85dc016197a9a2c91e36cc6eb4445fabf65fa9d` | `@sonic_swss//dist:swss_pkg` |
| libteam | `libteam@1.31.0.sonic.1`, including the existing 16 SONiC patch bodies | `@libteam//:libteam_runtime` and `@libteam//:libteamdctl_runtime` |
| SWSS Common | `sonic-swss-common@0.0.9-334389eae54afd1f4f5ee69f89c3a7ad2062ee61` | `@sonic_swss_common//:libswsscommon_pkg` |
| Sairedis | `sonic-sairedis@0.0.4-ee19458a7d1b417317aaf34fe48969da7293d670` | The owner's SAI runtime TARs |

`source_runtime` keeps Common's payload for consumers that also use this TAR
outside the shared image. The image checks still verify that the inherited
parent payload remains byte-identical. Direct libteam inputs preserve their
debug providers. Library-only owner metadata accounts for `libteam5` and
`libteamdctl0`, verifies their installed native bytes, and adds no utility payload.
The existing APT lock excludes those source-built libraries.

The root module selects the shared infrastructure interfaces used by the other
Docker families. `.bazelrc` keeps maintained registry `main` as its default.
Draft CI pins a complete registry catalog in the reviewed-source workflow
configuration; the workflow and `tools/bazel/ci/libteam_draft_bazel.sh` carry the
exact commit. Each download-helper context verifies its own full pinned SHA256.
Those transport alternatives preserve the source and package integrity checks.
A verified download is separate from configured-fetch and image acceptance.

Socat uses `1.8.0.2.sonic.6`. Its original source and Debian patch archive retain
separate hashes and extraction types. The source owner includes compiler-path
normalization and the procan namespace-string fix. Runtime/debug TAR contracts
and configured features remain those declared by that owner.

CI runs the owners' real native tests and checks archive contents, debug pairs,
package identities, inherited payloads, dependency coverage and installed
containers. The SWSS incremental probe edits the selected owner's exported
`fpmsyncd/bgp_eoiu_marker.py`, checks initial/no-op/edit/restore phases, and
restores the exact bytes and mode. Existing reviewed-source callers retain the
compatible probe entry point and cache behavior.

This remains a Draft integration until the exact combined catalog and image
revision pass native AMD64 and installed-runtime validation. Results from older
catalogs remain evidence for those revisions. Existing automatic architecture
matrices are retained; the owned Draft branch guards exclude DEB fixtures and
full VS CI. Remove temporary catalog selections and branch guards and pass the
normal required checks before marking the integration Ready.
