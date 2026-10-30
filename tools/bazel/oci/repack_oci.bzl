"""Preserve image configuration while upstream umoci creates a filesystem delta."""

def _impl(ctx):
    if len(ctx.files.tars) != 1:
        fail("repack_oci needs one complete configured filesystem")
    output = ctx.actions.declare_directory(ctx.label.name)
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:type"]
    args = ctx.actions.args()
    args.add("--base", ctx.file.base.path)
    args.add("--rootfs", ctx.files.tars[0])
    args.add("--platform", ctx.attr.platform)
    args.add("--umoci", ctx.executable._umoci)
    args.add("--tar", tar.tarinfo.binary)
    args.add("--output", output.path)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = [ctx.file.base] + ctx.files.tars,
        tools = tar.default.files.to_list() + [ctx.attr._umoci[DefaultInfo].files_to_run],
        outputs = [output],
        env = {"APT_INSTALL_BUILD_CONTAINER": ctx.attr.execution_environment},
        execution_requirements = {"no-sandbox": "1"},
        mnemonic = "RepackOCIWithUmoci",
    )
    return [DefaultInfo(files = depset([output]))]

repack_oci = rule(
    implementation = _impl,
    attrs = {
        "base": attr.label(allow_single_file = True, mandatory = True),
        "tars": attr.label_list(allow_files = True, mandatory = True, doc = "One complete filesystem; also traversed for source debug symbols."),
        "platform": attr.string(mandatory = True),
        "execution_environment": attr.string(mandatory = True),
        "_umoci": attr.label(default = Label(":umoci"), executable = True, cfg = "exec"),
        "_tool": attr.label(default = Label(":repack_oci"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:type"],
)
