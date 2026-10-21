"""Connect SONiC's checked APT selection to the shared package installer."""

def _apt_install_inputs_impl(ctx):
    packages = {}
    inputs = [ctx.file.selection]
    for key, target in ctx.attr.packages.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("%s must provide one original Debian archive" % target.label)
        packages[key] = files[0].path
        inputs.append(files[0])
    mapping = ctx.actions.declare_file(ctx.label.name + ".packages.json")
    ctx.actions.write(mapping, json.encode(packages) + "\n")
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    args = ctx.actions.args()
    args.add("--selection", ctx.file.selection)
    args.add("--packages", mapping)
    args.add("--output", manifest)
    args.add_all(ctx.files.additional_packages, before_each = "--additional-package")
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = inputs + [mapping] + ctx.files.additional_packages,
        outputs = [manifest],
        mnemonic = "SelectedDebianArchives",
        progress_message = "Verify locked Debian archives selected for %{label}",
    )
    return [DefaultInfo(files = depset([manifest]))]

apt_install_inputs = rule(
    doc = "Write the selected original DEBs for apt_install.packages_manifest.",
    implementation = _apt_install_inputs_impl,
    attrs = {
        "selection": attr.label(allow_single_file = [".json"], mandatory = True),
        "packages": attr.string_keyed_label_dict(
            allow_files = [".deb"],
            doc = "Lock key to the public rules_distroless package :deb target.",
            mandatory = True,
        ),
        "additional_packages": attr.label_list(
            allow_files = [".deb"],
            doc = "Source or retained archives installed in the same transaction as selected APT packages.",
        ),
        "_tool": attr.label(
            default = Label("//tools/bazel/oci:apt_install_inputs"),
            executable = True,
            cfg = "exec",
        ),
    },
)
