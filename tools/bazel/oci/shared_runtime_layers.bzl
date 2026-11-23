"""Produce one common payload and two deltas from already-checked image layers."""

def _impl(ctx):
    args = ctx.actions.args()
    for name, file in [
        ("first", ctx.file.first),
        ("second", ctx.file.second),
        ("common-out", ctx.outputs.common),
        ("first-out", ctx.outputs.first_delta),
        ("second-out", ctx.outputs.second_delta),
        ("receipt", ctx.outputs.receipt),
    ]:
        args.add("--" + name, file)
    args.add("--pigz", ctx.executable._pigz)
    ctx.actions.run(
        executable = ctx.executable._worker,
        arguments = [args],
        inputs = [ctx.file.first, ctx.file.second],
        tools = [ctx.attr._worker[DefaultInfo].files_to_run, ctx.attr._pigz[DefaultInfo].files_to_run],
        outputs = [ctx.outputs.common, ctx.outputs.first_delta, ctx.outputs.second_delta, ctx.outputs.receipt],
        mnemonic = "ShareVsAptRuntime",
    )
    return [DefaultInfo(files = depset([
        ctx.outputs.common,
        ctx.outputs.first_delta,
        ctx.outputs.second_delta,
        ctx.outputs.receipt,
    ]))]

shared_runtime_layers = rule(
    implementation = _impl,
    attrs = {
        "first": attr.label(allow_single_file = True, mandatory = True),
        "second": attr.label(allow_single_file = True, mandatory = True),
        "_worker": attr.label(
            default = Label(":shared_runtime_layers"),
            executable = True,
            cfg = "exec",
        ),
        "_pigz": attr.label(
            default = Label("@sonic_build_infra//tools/build_tools:pigz"),
            executable = True,
            cfg = "exec",
        ),
    },
    outputs = {
        "common": "%{name}/common.tar.gz",
        "first_delta": "%{name}/first.tar.gz",
        "second_delta": "%{name}/second.tar.gz",
        "receipt": "%{name}/receipt.json",
    },
)
