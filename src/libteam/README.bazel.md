# Shared libteam interfaces

The image consumes landed `libteam@1.31.0.sonic.1` through the compatibility
labels in `BUILD.bazel`. The shared owner builds upstream 1.31 with SONiC's
ordered 16 patches and Debian 1.31-1 metadata. The Make inputs and patch files
remain in this directory.

`libteam`, `libteamdctl`, their shared-library providers, the five utility
programs, runtime/debug TARs, development TAR and package metadata are aliases
to the shared module. SWSS keeps its existing image-root labels; Teamd binds
its package records to the shared module's actual MODULE file.

The coherent review catalog `04a71b7883384e995a5ae089e2b6f6a0601164de` adds only the exact landed libteam entry
from registry main `a1334b9a9604733a2faf6cc96536d289da09e8d8` to the previous
image catalog. Other selected module versions and locked package inputs stay
unchanged. The composition is a review input, not a claim that every other
image prerequisite has landed.

The native libteam workflow selects the owner's `@libteam//:consumer_test`
directly and resolves its exported `bazel/verify_rebuild.py` through the same
Bazel output base. Installed SDK checks require zero archive timestamps and
matching symbols. The shared owner retains the accepted deterministic gzip
and development-TAR packaging fixes, so old image payload hashes are not
claimed to be identical after adoption.

Current combined image validation remains pending. Earlier owner and PR #205
results retain their source attribution; they are not new Teamd networking,
OCI, LACP or Make/VS equivalence results.
