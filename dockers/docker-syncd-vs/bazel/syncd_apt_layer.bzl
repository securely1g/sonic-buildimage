"""Select locked APT content without replacing syncd's base or Make packages."""

load("@rules_distroless//apt/private:util.bzl", "util")
load("@tar.bzl//tar:tar.bzl", "tar_lib")

def _single_files(targets):
    result = {}
    for target, key in targets.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1 or key in result:
            fail("syncd APT content target must provide one unique file: " + str(target.label))
        result[key] = files[0]
    return result

def _syncd_apt_layer_impl(ctx):
    if not ctx.file.base.is_directory:
        fail("syncd_apt_layer base must be one OCI directory")
    payloads = _single_files(ctx.attr.payloads)
    controls = _single_files(ctx.attr.controls)
    if sorted(payloads) != sorted(controls):
        fail("syncd APT data and control target sets differ")
    mapping = {
        "locked": [
            {"key": key, "payload": payloads[key].path, "control": controls[key].path}
            for key in sorted(payloads)
        ],
        "hub_paths": sorted([file.path for file in ctx.files.hub]),
    }
    mapping_file = ctx.actions.declare_file(ctx.attr.name + ".inputs.json")
    ctx.actions.write(mapping_file, json.encode(mapping))
    output = ctx.actions.declare_file(ctx.attr.name + ".tar")
    receipt = ctx.actions.declare_file(ctx.attr.name + ".selection.json")
    bsdtar = ctx.toolchains[tar_lib.toolchain_type]
    args = ctx.actions.args()
    args.add("--base", ctx.file.base.path)
    args.add("--lock", ctx.file.lock.path)
    args.add("--make-manifest", ctx.file.make_manifest.path)
    args.add("--mapping", mapping_file.path)
    args.add("--variant", ctx.attr.variant)
    args.add("--bsdtar", bsdtar.tarinfo.binary.path)
    args.add("--out-tar", output.path)
    args.add("--receipt", receipt.path)
    ctx.actions.run(
        executable = ctx.attr._selector[DefaultInfo].files_to_run,
        arguments = [args],
        inputs = depset(payloads.values() + controls.values() + ctx.files.hub + [
            ctx.file.base,
            ctx.file.lock,
            ctx.file.make_manifest,
            mapping_file,
        ]),
        outputs = [output, receipt],
        tools = bsdtar.default.files,
        mnemonic = "SelectSyncdAptPayloads",
        progress_message = "Selecting syncd " + ctx.attr.variant + " APT payloads while retaining base packages",
    )
    return [
        DefaultInfo(files = depset([output])),
        OutputGroupInfo(selection = depset([receipt])),
    ]

_syncd_apt_layer = rule(
    implementation = _syncd_apt_layer_impl,
    attrs = {
        "base": attr.label(allow_single_file = True, mandatory = True),
        "lock": attr.label(allow_single_file = [".json"], mandatory = True),
        "make_manifest": attr.label(allow_single_file = [".json"], mandatory = True),
        "variant": attr.string(values = ["runtime", "debug"], mandatory = True),
        "hub": attr.label(allow_files = True, mandatory = True),
        "payloads": attr.label_keyed_string_dict(allow_files = True),
        "controls": attr.label_keyed_string_dict(allow_files = True),
        "_selector": attr.label(
            default = Label("//dockers/docker-syncd-vs:select_apt_payloads"),
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = [tar_lib.toolchain_type],
)

def syncd_apt_layer(name, variant, hub, package_keys, **kwargs):
    """Consume each locked package's existing Distroless data/control targets."""
    repository = Label(hub).repo_name
    dependency_set = {"runtime": "syncd_vs_debian", "debug": "syncd_vs_debug_debian"}[variant]
    if not repository.endswith(dependency_set):
        fail("unexpected syncd APT hub repository identity")
    prefix = repository[:-len(dependency_set)]
    _syncd_apt_layer(
        name = name,
        variant = variant,
        hub = hub,
        payloads = {
            "@@" + prefix + util.sanitize(key) + "//:data": key
            for key in package_keys
        },
        controls = {
            "@@" + prefix + util.sanitize(key) + "//:control": key
            for key in package_keys
        },
        **kwargs
    )
