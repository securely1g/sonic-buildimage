"""Run upstream umoci for the existing multi-layer Make OCI input."""

def _impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".tar")
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:type"]
    args = ctx.actions.args()
    args.add("--image", ctx.file.image.path)
    args.add("--platform", ctx.attr.platform)
    args.add("--umoci", ctx.executable._umoci)
    args.add("--tar", tar.tarinfo.binary)
    args.add("--output", output)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = [ctx.file.image],
        tools = tar.default.files.to_list() + [ctx.attr._umoci[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"APT_INSTALL_BUILD_CONTAINER": ctx.attr.execution_environment},
        execution_requirements = {"no-sandbox": "1"},
        mnemonic = "UnpackOCIWithUmoci",
    )
    return [DefaultInfo(files = depset([output]))]

unpack_oci = rule(
    implementation = _impl,
    attrs = {
        "image": attr.label(allow_single_file = True, mandatory = True),
        "platform": attr.string(mandatory = True),
        "execution_environment": attr.string(mandatory = True),
        "_umoci": attr.label(default = Label(":umoci"), executable = True, cfg = "exec"),
        "_tool": attr.label(default = Label(":unpack_oci"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:type"],
)
