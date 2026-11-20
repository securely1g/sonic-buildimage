"""Compress a declared, private snapshot with the build slave's captured ELF runtime."""

def _compress(ctx):
    image = ctx.actions.declare_file("root.squashfs")
    loader = ctx.file.loader
    libraries = loader.dirname + "/lib"
    # Feed the declared archive through stdin. The shell and compressor both run
    # with their captured loader and shared libraries, independent of the host.
    ctx.actions.run(
        executable = loader,
        inputs = ctx.files.inputs + [ctx.file.archive, ctx.file.attributes],
        outputs = [image],
        arguments = [
            "--inhibit-cache", "--library-path", libraries, loader.dirname + "/sh",
            "-c", 'input=$1; shift; exec "$@" < "$input"', "compress",
            ctx.file.archive.path, loader.path, "--inhibit-cache", "--library-path", libraries,
            loader.dirname + "/mksquashfs", "-", image.path, "-tar", "-noappend",
            "-exit-on-error", "-no-progress", "-exports", "-no-tailends", "-comp", "gzip",
            "-Xcompression-level", "1", "-b", "128K", "-pf", ctx.file.attributes.path,
        ] + ctx.attr.root_options,
        execution_requirements = {"no-remote-exec": "1", "block-network": "1"},
        mnemonic = "SonicSquashfs",
    )
    return DefaultInfo(files = depset([image]))

private_squashfs = rule(
    implementation = _compress,
    attrs = {
        "archive": attr.label(allow_single_file = True, mandatory = True),
        "attributes": attr.label(allow_single_file = True, mandatory = True),
        "inputs": attr.label_list(allow_files = True, mandatory = True),
        "loader": attr.label(allow_single_file = True, mandatory = True),
        "root_options": attr.string_list(mandatory = True),
    },
)
