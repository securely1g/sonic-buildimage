# Shared libteam interfaces

The image consumes landed `libteam@1.31.0.sonic.1` through the compatibility
labels in `BUILD.bazel`. The shared owner builds upstream 1.31 with SONiC's
ordered 16 patches and Debian 1.31-1 metadata. The Make inputs and patch files
remain in this directory.

`libteam`, `libteamdctl`, their shared-library providers, the five utility
programs, runtime/debug TARs, development TAR and package metadata are aliases
to the shared module. SWSS keeps its existing image-root labels; Teamd binds
its package records to the shared module's actual MODULE file.

The coherent review catalog `c2d8152ea91cc543547d0078c19928cd6f082417` retains
the exact landed libteam entry added by parent catalog
`04a71b7883384e995a5ae089e2b6f6a0601164de` from registry main
`a1334b9a9604733a2faf6cc96536d289da09e8d8`. It also refreshes dependency metadata
and documentation for the existing `net-snmp@5.9.3.sonic.1` registration; that
module's version, source archives, patches and TAR/test contract are unchanged.
The libteam and Net-SNMP contracts remain unchanged. The root now selects
`p4lang-pi@0.1.3.sonic.3`,
`sonic-build-infra@0.0.28-58112470001b1c450fd9e859079e92c62c236242`,
`sonic-fips@0.0.6-b7a5b1903d0a6bf11fd5283bd6eaba0c15b899f0`,
`rules_distroless@0.9.4.sonic.18` and `tar.bzl@0.10.5.sonic.2`. Combined image
validation of this selection remains pending. The composition is a review
input, not a claim that every other image prerequisite has landed.
That FIPS source retains the repaired libedit metadata lookup and strict
source OpenSSL header checks.

The root imports the shared source, requests and generated APT lock from
that Infra version, including the FRR and Net-SNMP requests declared by the
root and Bookworm extension. The owner checker and checks over retained
consumer declarations passed; combined image and installed-runtime
acceptance remain pending.

The native libteam workflow selects the owner's `@libteam//:consumer_test`
directly and resolves its exported `bazel/verify_rebuild.py` through the same
Bazel output base. Installed SDK checks require zero archive timestamps and
matching symbols. The shared owner retains the accepted deterministic gzip
and development-TAR packaging fixes, so old image payload hashes are not
claimed to be identical after adoption.

Current combined image validation remains pending. Earlier owner and PR #205
results retain their source attribution; they are not new Teamd networking,
OCI, LACP or Make/VS equivalence results.
