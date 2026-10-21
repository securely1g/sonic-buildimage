"""Build the existing image validation contract from producer-owned metadata."""

def _impl(ctx):
    output = ctx.actions.declare_file(ctx.attr.out)
    args = ctx.actions.args()
    if ctx.attr.image:
        args.add("--image", ctx.attr.image)
    args.add("--architecture", ctx.attr.architecture)
    if ctx.attr.distribution:
        args.add("--distribution", ctx.attr.distribution)
    args.add("--output", output)
    args.add("--mode", ctx.attr.mode)
    inputs = []
    if ctx.file.expectations:
        inputs.append(ctx.file.expectations)
        args.add("--expectations", ctx.file.expectations)
    if ctx.file.root_module:
        inputs.append(ctx.file.root_module)
        args.add("--root-module", ctx.file.root_module)
    for name, module in ctx.attr.module_sources.items():
        args.add("--module-source", name + "=" + module)
    for target in ctx.attr.owner_metadata:
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("owner metadata must provide one JSON file")
        inputs.extend(files)
        args.add("--metadata", str(target.label) + "=" + files[0].path)
    for name, target in ctx.attr.source_modules.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("owner module must provide one MODULE.bazel file")
        inputs.extend(files)
        args.add("--module", name + "=" + files[0].path)
    for name, target in ctx.attr.sources.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("source package must provide one runtime TAR")
        inputs.extend(files)
        args.add("--payload", name + "=" + files[0].path)
        args.add("--source", name + "=" + ctx.attr.declared_sources[name])
        args.add("--public-source", name + "=" + ctx.attr.public_sources[name])
    ctx.actions.run(
        executable = ctx.executable._tool,
        inputs = inputs,
        outputs = [output],
        arguments = [args],
        mnemonic = "SourcePackageContract",
    )
    return [DefaultInfo(files = depset([output]))]

_contract = rule(
    implementation = _impl,
    attrs = {
        "image": attr.string(),
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64"]),
        "distribution": attr.string(),
        "mode": attr.string(default = "contract", values = ["contract", "provenance"]),
        "expectations": attr.label(allow_single_file = [".json"]),
        "owner_metadata": attr.label_list(allow_files = [".json"]),
        "source_modules": attr.string_keyed_label_dict(allow_files = True),
        "module_sources": attr.string_dict(doc = "Package-to-module identities for provenance-only owners without package metadata."),
        "root_module": attr.label(allow_single_file = True, doc = "Actual root MODULE.bazel containing pinned git_override selections."),
        "sources": attr.string_keyed_label_dict(allow_files = True),
        "public_sources": attr.string_dict(),
        "declared_sources": attr.string_dict(),
        "out": attr.string(default = "source_packages.json"),
        "_tool": attr.label(default = Label("//tools/bazel/oci:source_contract"), executable = True, cfg = "exec"),
    },
)

def source_contract(name, sources, **kwargs):
    """Keep public target spelling while checking Bazel's resolved target identity."""
    _contract(
        name = name,
        sources = sources,
        public_sources = sources,
        declared_sources = {name: str(native.package_relative_label(target)) for name, target in sources.items()},
        **kwargs
    )
