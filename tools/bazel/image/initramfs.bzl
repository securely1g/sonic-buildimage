"""SONiC initramfs generation from declared rootfs layers and kernel identity."""


def _initramfs_impl(ctx):
    output = ctx.actions.declare_file(ctx.attr.out)
    report = ctx.actions.declare_file(ctx.label.name + ".audit.json")
    request = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    layers = []
    for target in ctx.attr.layers:
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("initramfs layer must provide exactly one TAR: %s" % target.label)
        layers.append(files[0])
    if ctx.attr.compression == "zstd" and not ctx.attr.zstd:
        fail("zstd compression requires zstd")
    ctx.actions.write(request, json.encode({
        "layers": [layer.path for layer in layers],
        "kernel_metadata": ctx.file.kernel_metadata.path,
        "config_dir": ctx.attr.config_dir,
        "extra_config": [file.path for file in ctx.files.extra_config],
        "compression": ctx.attr.compression,
        "compression_level": ctx.attr.compression_level,
        "modules": ctx.attr.modules,
        "modprobe": ctx.executable.modprobe.path,
        "depmod": ctx.executable.depmod.path,
        "zstd": ctx.executable.zstd.path if ctx.attr.zstd else None,
        "busybox_applets": ctx.file.busybox_applets.path,
    }))
    ctx.actions.run(
        executable = ctx.attr._tool[DefaultInfo].files_to_run,
        arguments = [request.path, output.path, report.path],
        inputs = [request, ctx.file.kernel_metadata, ctx.file.busybox_applets] + layers + ctx.files.extra_config,
        outputs = [output, report],
        tools = [tool[DefaultInfo].files_to_run for tool in [ctx.attr.modprobe, ctx.attr.depmod, ctx.attr.zstd] if tool],
        env = {"LC_ALL": "C", "TZ": "UTC", "PYTHONHASHSEED": "0"},
        mnemonic = "SonicInitramfs",
        progress_message = "Generating SONiC initramfs %{label}",
    )
    return [DefaultInfo(files = depset([output])), OutputGroupInfo(audit = depset([report]))]


sonic_initramfs = rule(
    implementation = _initramfs_impl,
    doc = "Generate initrd from the caller-selected boot or kdump rootfs state; never execute target programs.",
    attrs = {
        "layers": attr.label_list(allow_files = True, mandatory = True),
        "kernel_metadata": attr.label(allow_single_file = [".json"], mandatory = True),
        "config_dir": attr.string(default = "/etc/initramfs-tools"),
        "extra_config": attr.label_list(allow_files = True),
        "compression": attr.string(default = "zstd", values = ["zstd", "gzip", "xz"]),
        "compression_level": attr.int(default = -1),
        "modules": attr.string(default = "most", values = ["most", "list"]),
        "out": attr.string(mandatory = True),
        "busybox_applets": attr.label(allow_single_file = [".json"], mandatory = True),
        "modprobe": attr.label(executable = True, cfg = "exec", mandatory = True),
        "depmod": attr.label(executable = True, cfg = "exec", mandatory = True),
        "zstd": attr.label(executable = True, cfg = "exec"),
        "_tool": attr.label(default = Label(":mkinitramfs"), executable = True, cfg = "exec"),
    },
)
