# Retained P4 source descriptors

These are byte-for-byte copies of the upstream OpenSUSE descriptors pinned in
[`../source-inputs.json`](../source-inputs.json). The manifest records the official
CI retrieval runs and their provenance. `tools/ci/p4_source_inputs.py` validates
each descriptor against its existing size and SHA-256 before staging it.

The six corresponding archives are already published in the registry's
[`p4-source-inputs-20261021` release](https://github.com/securely1g/sonic-bazel-registry/releases/tag/p4-source-inputs-20261021).
Retaining these small metadata files allows the same pinned inputs to be verified
when the OpenSUSE endpoint is unavailable. The known P4C descriptor/archive
inconsistency remains reported by the verifier; these files are not rewritten.
