"""Apply SONiC's dpkg exclusions when assembling OCI layers."""

load("@sonic_build_infra//oci:layer.bzl", "assemble_layer")

_EXCLUDES = "//tools/bazel/dpkg:dpkg_excludes.txt"

# bsdtar can read --excludes from a file, but not includes.
# So, we need to list the arguments by hand.
# This is kept in sync with dpkg_01_drop via //tools/bazel/tests:test_dpkg_patterns_up_to_date.
PATH_INCLUDES = [
    "./usr/share/doc/*/copyright",
    "usr/share/doc/*/copyright",
]

# Source TARs retain upstream license names rather than Debian's "copyright".
# Keep these notices in Bazel OCI layers without changing Make's dpkg policy.
# Both spellings are needed because bsdtar matches the original member name.
_OCI_NOTICE_INCLUDES = [
    prefix + name
    for prefix in ["./usr/share/doc/*/", "usr/share/doc/*/"]
    for name in [
        "LICENSE*",
        "LICENCE*",
        "*-LICENSE*",
        "*-LICENCE*",
        "COPYING*",
        "NOTICE*",
        "license*",
        "licence*",
        "*-license*",
        "*-licence*",
        "copying*",
        "notice*",
    ]
]

# The layer producer and receipt validator consume this same include policy.
OCI_INCLUDES = PATH_INCLUDES + _OCI_NOTICE_INCLUDES

def dpkg_filter(name, src, tars = [], debug_symbol_deps = [], excludes = [_EXCLUDES], includes = OCI_INCLUDES, **kwargs):
    """Filter a single layer while propagating debug metadata from its owners."""
    assemble_layer(
        name = name,
        archives = [src],
        debug_symbol_deps = tars + debug_symbol_deps,
        excludes = excludes,
        includes = includes,
        **kwargs
    )

def sonic_layer(name, tars, deduplicate = True, excludes = [_EXCLUDES], includes = OCI_INCLUDES, **kwargs):
    """Assemble package archives using the SONiC container filter policy."""
    # The final OCI layer always has unique paths. Keep the legacy argument
    # for callers that previously disabled deduplication only during flatten.
    _ = deduplicate
    assemble_layer(
        name = name,
        archives = tars,
        excludes = excludes,
        includes = includes,
        **kwargs
    )
