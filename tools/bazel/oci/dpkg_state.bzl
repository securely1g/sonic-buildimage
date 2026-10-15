"""Maintain Debian package ownership alongside declaratively assembled payloads.

Distroless imports the existing Debian data/control archives. This action writes
package metadata only; the upstream BSD tar rule packages that metadata into an
OCI layer. It does not build DEBs or execute package scripts on the build host.
"""

load("@tar.bzl", "tar")

def _one(target):
    files = target[DefaultInfo].files.to_list()
    if len(files) != 1:
        fail("%s must provide exactly one file" % target.label)
    return files[0]

def _dpkg_state_impl(ctx):
    tree = ctx.actions.declare_directory(ctx.label.name + ".files")
    mtree = ctx.actions.declare_file(ctx.label.name + ".mtree")
    receipt = ctx.actions.declare_file(ctx.label.name + ".json")
    inputs_file = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    inputs = [ctx.file.base, ctx.file.status, ctx.file.apt_receipt, ctx.file.source_controls]
    apt = []
    if len(ctx.attr.apt_payloads) != len(ctx.attr.apt_controls):
        fail("APT payload and control counts differ")
    for data, control in zip(ctx.attr.apt_payloads, ctx.attr.apt_controls):
        payload = _one(data)
        metadata = _one(control)
        inputs.extend([payload, metadata])
        apt.append({"payload": payload.path, "control": metadata.path})
    imported = []
    if len(ctx.attr.imported_payloads) != len(ctx.attr.imported_controls):
        fail("Imported payload and control counts differ")
    for data, control in zip(ctx.attr.imported_payloads, ctx.attr.imported_controls):
        payload = _one(data)
        metadata = _one(control)
        inputs.extend([payload, metadata])
        imported.append({"payload": payload.path, "control": metadata.path})
    sources = []
    for name, target in ctx.attr.sources.items():
        payload = _one(target)
        inputs.append(payload)
        sources.append({"package": name, "payload": payload.path})
    inputs.extend(ctx.files.control_files)
    inputs.extend(ctx.files.payload_layers)
    document = {
        "architecture": ctx.attr.architecture,
        "apt": apt,
        "imported": imported,
        "sources": sources,
        "source_controls": ctx.file.source_controls.path,
        "control_files": [file.path for file in ctx.files.control_files],
        "payload_layers": [file.path for file in ctx.files.payload_layers],
        "normalization": json.decode(ctx.attr.normalization),
        "receipt_path": ctx.attr.receipt_path,
        "extra_owned_paths": ctx.attr.extra_owned_paths,
    }
    ctx.actions.write(inputs_file, json.encode(document))
    args = ctx.actions.args()
    args.add("--base", ctx.file.base.path)
    args.add("--status", ctx.file.status)
    args.add("--apt-receipt", ctx.file.apt_receipt)
    args.add("--inputs", inputs_file)
    args.add("--out-dir", tree.path)
    args.add("--mtree", mtree)
    args.add("--receipt", receipt)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = inputs + [inputs_file],
        outputs = [tree, mtree, receipt],
        mnemonic = "DebianPackageState",
        progress_message = "Record Debian package ownership and lifecycle metadata for %{label}",
    )
    return [
        DefaultInfo(files = depset([tree])),
        OutputGroupInfo(mtree = depset([mtree]), receipt = depset([receipt])),
    ]

_dpkg_state = rule(
    implementation = _dpkg_state_impl,
    attrs = {
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64"]),
        "base": attr.label(mandatory = True, allow_single_file = True),
        "status": attr.label(mandatory = True, allow_single_file = True),
        "apt_receipt": attr.label(mandatory = True, allow_single_file = True),
        "apt_payloads": attr.label_list(allow_files = True),
        "apt_controls": attr.label_list(allow_files = True),
        "imported_payloads": attr.label_list(allow_files = True),
        "imported_controls": attr.label_list(allow_files = True),
        "sources": attr.string_keyed_label_dict(allow_files = True),
        "payload_layers": attr.label_list(allow_files = True),
        "extra_owned_paths": attr.string_list_dict(),
        "source_controls": attr.label(
            default = Label("//tools/bazel/dpkg/source_controls:manifest.json"),
            allow_single_file = True,
        ),
        "control_files": attr.label_list(default = [Label("//tools/bazel/dpkg/source_controls:files")], allow_files = True),
        "normalization": attr.string(default = "{}"),
        "receipt_path": attr.string(mandatory = True),
        "_tool": attr.label(default = Label("//tools/bazel/oci:dpkg_state"), executable = True, cfg = "exec"),
    },
)

def dpkg_state(name, visibility = None, **kwargs):
    """Write complete package records for the declared image payloads."""
    _dpkg_state(name = name + "_files", **kwargs)
    native.filegroup(
        name = name + "_mtree",
        srcs = [":" + name + "_files"],
        output_group = "mtree",
    )
    native.filegroup(
        name = name + "_receipt",
        srcs = [":" + name + "_files"],
        output_group = "receipt",
        visibility = visibility,
    )
    tar(
        name = name,
        srcs = [":" + name + "_files"],
        mtree = ":" + name + "_mtree",
        visibility = visibility,
    )
