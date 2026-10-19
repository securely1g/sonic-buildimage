"""Read-only source provenance for a filesystem already configured by APT."""

def _impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:type"]
    args = ctx.actions.args()
    inputs = [ctx.file.rootfs, ctx.file.installation, ctx.file.spec]
    inputs.append(ctx.file.path_filters)
    args.add("--path-filters", ctx.file.path_filters)
    for name in ["rootfs", "installation", "spec"]:
        args.add("--" + name, getattr(ctx.file, name))
    if ctx.file.inherited_metadata:
        args.add("--inherited-metadata", ctx.file.inherited_metadata)
        args.add("--inherited-metadata-path", ctx.attr.inherited_metadata_path)
        inputs.append(ctx.file.inherited_metadata)
    if ctx.file.selection:
        args.add("--selection", ctx.file.selection)
        inputs.append(ctx.file.selection)
    if ctx.file.runtime_layer:
        args.add("--runtime-layer", ctx.file.runtime_layer)
        inputs.append(ctx.file.runtime_layer)
    if ctx.attr.allow_same_owner_overlays:
        args.add("--allow-same-owner-overlays")
    args.add("--architecture", ctx.attr.architecture)
    args.add("--output", output)
    args.add("--tar", tar.tarinfo.binary)
    for name, target in ctx.attr.packages.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("Expected one actual Debian package for " + name)
        args.add("--package", name + "=" + files[0].path)
        inputs.extend(files)
    for name, target in ctx.attr.payloads.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("Expected one runtime TAR for " + name)
        args.add("--payload", name + "=" + files[0].path)
        inputs.extend(files)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = inputs,
        tools = tar.default.files,
        outputs = [output],
        mnemonic = "VerifyInstalledSources",
    )
    return [DefaultInfo(files = depset([output]))]

installed_source_metadata = rule(
    implementation = _impl,
    attrs = {
        "rootfs": attr.label(allow_single_file = True, mandatory = True),
        "installation": attr.label(allow_single_file = True, mandatory = True),
        "inherited_metadata": attr.label(allow_single_file = True),
        "inherited_metadata_path": attr.string(),
        "selection": attr.label(allow_single_file = True, doc = "Runtime APT selection to verify against installed dependency controls."),
        "spec": attr.label(allow_single_file = True, mandatory = True),
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64"]),
        "packages": attr.string_keyed_label_dict(allow_files = True, mandatory = True),
        "payloads": attr.string_keyed_label_dict(allow_files = True, doc = "Source runtime TARs; no Debian package identity is invented for them."),
        "runtime_layer": attr.label(allow_single_file = True, doc = "Normalized runtime TAR applied after APT, or omitted when inherited from the runtime image."),
        "allow_same_owner_overlays": attr.bool(default = False, doc = "Make-base compatibility only: an ordinary source TAR may replace files owned by its same-named installed package."),
        "path_filters": attr.label(default = Label("//dockers/docker-base-trixie:dpkg_01_drop"), allow_single_file = True),
        "_tool": attr.label(default = Label(":installed_source_metadata"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:type"],
)
