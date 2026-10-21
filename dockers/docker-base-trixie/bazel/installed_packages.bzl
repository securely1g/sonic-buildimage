"""Record APT and source package identities without producing Debian packages."""

def _installed_packages_impl(ctx):
    status = ctx.actions.declare_file(ctx.label.name + ".status")
    receipt = ctx.actions.declare_file(ctx.label.name + ".json")
    args = ctx.actions.args()
    args.add("--base", ctx.file.base.path)
    args.add("--apt-receipt", ctx.file.apt_receipt)
    args.add("--source-spec", ctx.file.source_spec)
    if ctx.file.normalization_receipt:
        args.add("--normalization-receipt", ctx.file.normalization_receipt)
    args.add("--architecture", ctx.attr.architecture)
    args.add("--status", status)
    args.add("--receipt", receipt)
    args.add_all(ctx.files.controls, before_each = "--control")
    retained = []
    if ctx.file.retained_manifest:
        retained = [ctx.file.retained_manifest]
        args.add("--retained-manifest", ctx.file.retained_manifest)
    source_receipt = []
    if ctx.file.source_receipt:
        args.add("--source-receipt", ctx.file.source_receipt)
        source_receipt = [ctx.file.source_receipt]
    payloads = []
    for name, target in ctx.attr.sources.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("source package %s must provide one runtime TAR" % name)
        payloads.append(files[0])
        args.add("--source", name + "=" + files[0].path)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = [ctx.file.base, ctx.file.apt_receipt, ctx.file.source_spec] + ctx.files.controls + payloads + retained + source_receipt + ctx.files.normalization_receipt,
        outputs = [status, receipt],
        mnemonic = "InstalledPackageMetadata",
        progress_message = "Validate installed source and Debian package identities for %{label}",
    )
    return [
        DefaultInfo(files = depset([status])),
        OutputGroupInfo(metadata = depset([receipt])),
    ]

installed_packages = rule(
    implementation = _installed_packages_impl,
    attrs = {
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64"]),
        "apt_receipt": attr.label(allow_single_file = True, mandatory = True),
        "base": attr.label(allow_single_file = True, mandatory = True),
        "controls": attr.label_list(allow_files = True),
        "source_receipt": attr.label(allow_single_file = [".json"]),
        "source_spec": attr.label(allow_single_file = [".json"], mandatory = True),
        "retained_manifest": attr.label(allow_single_file = True),
        "normalization_receipt": attr.label(
            allow_single_file = [".json"],
            doc = "Receipt from this source layer's normalize_layer action; bound to the same base.",
        ),
        "sources": attr.string_keyed_label_dict(allow_files = True),
        "_tool": attr.label(default = Label(":installed_packages"), executable = True, cfg = "exec"),
    },
)
