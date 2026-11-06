# Dedicated-worker image validation

The normal P4RT and Syncd PR jobs can verify a dedicated worker's results when
the hosted runner cannot hold the source build. The existing image producers,
validation commands and cache locations remain the build interface.

For a same-repository PR, the first normal CI attempt saves a small immutable
request artifact. It identifies the exact merge commit and tree, PR head and
base, registry catalog, reviewed worker controller and validation contract.
P4RT additionally binds its source-owner gitlink. A retry must reuse that
original request; it cannot replace it with a different checkout.

After reviewing the request and the worker's available resources, dispatch the
existing `Bazel OCI` workflow with `source_image=p4rt` or `source_image=syncd`,
the requested controller revision, exact merge as `source_revision`, requested
catalog as `registry_revision`, and original artifact ID as
`source_ci_request`. Select one of the dedicated workers explicitly. The worker
checks those identities before building and preserves its existing caches.

Syncd runs the existing build, contents/symbols, incremental and loaded-service
phases. P4RT also runs its SDK preparation and complete owner test suite. The
worker uploads a bounded JSON acceptance artifact after its private Docker
daemon stops. Normal CI requires the whole worker job, checkout cleanup and
workflow to finish successfully before accepting that artifact.

Rerun the failed normal build job after the worker completes. Its verifier
checks the original request, controller and worker identity, receipt hashes,
source graph, output inventory, no-DEB action audit and all profile-specific
results. The dependent runtime job verifies the same acceptance independently.
A missing receipt or a changed PR head/base fails the check; it is not a skip.
P4RT and Syncd use separate artifact names and request schemas, so evidence
from one cannot satisfy the other.

The JSON hashes are checked locally by the verifier. Large TAR/image payload
hashes and native runtime results remain reported by the pinned CI worker.
These checks do not establish ASIC forwarding or full VS installer acceptance.
Fork PRs, maintained-branch builds and manual non-receipt jobs retain the
existing hosted build and runtime steps.

The implementation is `image_worker_receipt.py`. The old
`p4rt_worker_receipt.py` command remains a compatibility entry point; its
existing schema and artifact names are preserved.
