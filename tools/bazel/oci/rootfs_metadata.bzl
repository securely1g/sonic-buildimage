"""Preserve source debug references when APT produces a complete filesystem."""

def _rootfs_metadata_impl(ctx):
    # No extraction or assembly happens here. The file comes from upstream
    # apt_install/flatten; the tars edges let debug_symbols_layer find symbols
    # from both source runtime TARs and Debian-packaged replacements.
    return [DefaultInfo(files = depset([ctx.file.src]))]

rootfs_metadata = rule(
    implementation = _rootfs_metadata_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "tars": attr.label_list(allow_files = True, doc = "Debug metadata sources; these files are not copied into the filesystem."),
    },
)
