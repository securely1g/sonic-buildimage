"""Use Debian's tool to register the image's declared command alternatives."""

load("@tar.bzl", "tar")

def _alternatives_state_impl(ctx):
    tree = ctx.actions.declare_directory(ctx.label.name + ".files")
    mtree = ctx.actions.declare_file(ctx.label.name + ".mtree")
    receipt = ctx.actions.declare_file(ctx.label.name + ".json")
    inputs_file = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    groups = json.decode(ctx.attr.groups)
    packages = []
    inputs = [ctx.file.base, inputs_file]
    if sorted(ctx.attr.controls) != sorted(ctx.attr.payloads) or sorted(groups) != sorted(ctx.attr.payloads):
        fail("Alternative providers must have matching controls, payloads and groups")
    for name in sorted(groups):
        control = ctx.attr.controls[name][DefaultInfo].files.to_list()
        payload = ctx.attr.payloads[name][DefaultInfo].files.to_list()
        if len(control) != 1 or len(payload) != 1:
            fail("Alternative provider must expose one data and one control archive")
        inputs.extend(control + payload)
        packages.append({"package": name, "control": control[0].path, "payload": payload[0].path, "groups": groups[name]})
    ctx.actions.write(inputs_file, json.encode({"architecture": ctx.attr.architecture, "packages": packages}))
    bsdtar = ctx.toolchains["@tar.bzl//tar/toolchain:type"]
    args = ctx.actions.args()
    args.add(bsdtar.tarinfo.binary.path)
    args.add(ctx.executable._tool.path)
    args.add(ctx.file.base.path)
    args.add(inputs_file.path)
    args.add(tree.path)
    args.add(mtree.path)
    args.add(receipt.path)
    args.add_all(ctx.files.tool_payloads)

    # BSD tar extracts pinned execution tools into a private temporary root.
    # The Python helper invokes update-alternatives against a separate temporary
    # image root. Neither command can update the build host's alternatives.
    ctx.actions.run_shell(
        arguments = [args],
        command = """set -eu
bsdtar="$1"; helper="$2"; base="$3"; inputs="$4"; output="$5"; mtree="$6"; receipt="$7"
shift 7
tool_root=$(mktemp -d)
trap 'rm -rf "$tool_root"' EXIT
for archive in "$@"; do
    "$bsdtar" --no-same-owner -xf "$archive" -C "$tool_root"
done
"$helper" --base "$base" --inputs "$inputs" --tool-root "$tool_root" \
    --out-dir "$output" --mtree "$mtree" --receipt "$receipt"
""",
        inputs = depset(inputs + ctx.files.tool_payloads, transitive = [bsdtar.default.files]),
        tools = [ctx.attr._tool[DefaultInfo].files_to_run, bsdtar.tarinfo.binary],
        outputs = [tree, mtree, receipt],
        mnemonic = "DebianCommandAlternatives",
        progress_message = "Register Debian command alternatives for %{label}",
    )
    return [DefaultInfo(files = depset([tree])), OutputGroupInfo(mtree = depset([mtree]), receipt = depset([receipt]))]

_alternatives_state = rule(
    implementation = _alternatives_state_impl,
    attrs = {
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64"]),
        "base": attr.label(mandatory = True, allow_single_file = True),
        "controls": attr.string_keyed_label_dict(allow_files = True),
        "payloads": attr.string_keyed_label_dict(allow_files = True),
        "groups": attr.string(mandatory = True),
        "tool_payloads": attr.label_list(mandatory = True, allow_files = True, cfg = "exec"),
        "_tool": attr.label(default = Label("//tools/bazel/oci:alternatives_state"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:type"],
)

def alternatives_state(name, **kwargs):
    """Package only the state and command links emitted by update-alternatives."""
    _alternatives_state(name = name + "_files", **kwargs)
    native.filegroup(name = name + "_mtree", srcs = [":" + name + "_files"], output_group = "mtree")
    native.filegroup(name = name + "_receipt", srcs = [":" + name + "_files"], output_group = "receipt")
    tar(name = name, srcs = [":" + name + "_files"], mtree = ":" + name + "_mtree")
