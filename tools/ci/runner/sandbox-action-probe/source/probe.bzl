def _probe_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = "/usr/bin/python3",
        inputs = [ctx.file._script],
        outputs = [output],
        arguments = [ctx.file._script.path, ctx.attr.guard, ctx.attr.parent_netns, output.path],
        mnemonic = "SonicKernelBuild",
        execution_requirements = {"no-network": "1", "no-cache": "1", "no-remote": "1"},
    )
    return [DefaultInfo(files = depset([output]))]
sandbox_probe = rule(
    implementation = _probe_impl,
    attrs = {
        "guard": attr.string(mandatory = True),
        "parent_netns": attr.string(mandatory = True),
        "_script": attr.label(default = "//:probe.py", allow_single_file = True),
    },
)
