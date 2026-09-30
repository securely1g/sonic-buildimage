"""Cacheable SONiC VS image assembly from explicit phase-one native inputs.

Only host finalization and Docker import need a privileged, pinned local worker.
Their outputs participate in Bazel's normal action cache; no mutable store is
reused between actions. Metadata projections keep binary edits off the host's
dependency edge. Final packaging uses ordinary unprivileged actions.
"""

HostInfo = provider("Finalized host filesystem and boot payloads.", fields = ["fs", "boot", "platform"])

def _import_resources(_os, _input_size):
    return {"cpu": 4, "memory": 2048}

def _python_action(ctx, script, args, inputs, outputs, mnemonic, privileged = False):
    ctx.actions.run_shell(
        command = 'cmp -- "$1" /run/sonic-image-worker.json && shift && exec /usr/bin/python3 "$@"',
        arguments = [ctx.file.execution_environment.path, script.path] + args,
        inputs = depset(inputs + [script, ctx.file.execution_environment]),
        outputs = outputs,
        mnemonic = mnemonic,
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC"},
        execution_requirements = {"no-sandbox": "1", "no-remote-exec": "1"} if privileged else {"no-remote-exec": "1"},
        resource_set = _import_resources if privileged else None,
    )

def _metadata_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    _python_action(
        ctx,
        ctx.file._script,
        ["--oci" if ctx.attr.oci else "--archive", ctx.file.src.path, "--output", output.path],
        [ctx.file.src],
        [output],
        "SonicServiceMetadata",
    )
    return [DefaultInfo(files = depset([output]))]

service_metadata = rule(
    implementation = _metadata_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "oci": attr.bool(default = False),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":metadata.py"), allow_single_file = True),
    },
)

def _store_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name + ".store")
    args = ctx.actions.args()
    args.add(ctx.file._import)
    args.add("--archive", ctx.file.src)
    args.add("--output", output.path)
    args.add("--collector", ctx.file._collector)
    args.add("--execution-environment", ctx.file.execution_environment)
    args.add("--jobs", ctx.attr.jobs)
    for tag in ctx.attr.tags_to_add:
        args.add("--tag", tag)
    ctx.actions.run_shell(
        command = 'exec sudo -n unshare --mount --pid --fork --kill-child --mount-proc --net /usr/bin/python3 "$@" --uid "$(id -u)" --gid "$(id -g)"',
        arguments = [args],
        inputs = [ctx.file.src, ctx.file._import, ctx.file._collector, ctx.file.execution_environment],
        outputs = [output],
        mnemonic = "SonicDockerImport",
        resource_set = _import_resources,
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        execution_requirements = {"no-sandbox": "1", "no-remote-exec": "1"},
    )
    return [DefaultInfo(files = depset([output]))]

docker_store_part = rule(
    implementation = _store_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "tags_to_add": attr.string_list(),
        "jobs": attr.int(default = 8),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_import": attr.label(default = Label(":import_store.py"), allow_single_file = True),
        "_collector": attr.label(default = Label(":store.py"), allow_single_file = True),
    },
)

def _merge_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".tar.gz")
    args = ["merge", "--output", output.path]
    for part in ctx.files.parts:
        args += ["--part", part.path]
    _python_action(ctx, ctx.file._script, args, ctx.files.parts, [output], "SonicDockerStore")
    return [DefaultInfo(files = depset([output]))]

docker_store = rule(
    implementation = _merge_impl,
    attrs = {
        "parts": attr.label_list(allow_files = True, mandatory = True),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":store.py"), allow_single_file = True),
    },
)

def _host_impl(ctx):
    outputs = {name: ctx.actions.declare_file(ctx.label.name + "." + suffix) for name, suffix in {
        "fs": "squashfs",
        "boot": "boot.tar",
        "platform": "platform.tar.gz",
        "receipt": "receipt.json",
    }.items()}
    args = []
    inputs = [ctx.file._metadata]
    for flag, file in {
        "source": ctx.file.source,
        "snapshot": ctx.file.snapshot,
        "config": ctx.file.config,
        "execution-environment": ctx.file.execution_environment,
        "build-script": ctx.file.build_script,
        "extension-template": ctx.file.extension_template,
    }.items():
        args += ["--" + flag, file.path]
        inputs.append(file)
    for flag, targets in [("metadata", ctx.attr.metadata), ("local-image", ctx.attr.local_images)]:
        for target, name in targets.items():
            files = target[DefaultInfo].files.to_list()
            if len(files) != 1:
                fail("host image inputs must contain exactly one file")
            args += ["--" + flag, name + "=" + files[0].path]
            inputs += files
    for name, output in outputs.items():
        args += ["--" + name, output.path]
    _python_action(ctx, ctx.file._script, args, inputs, outputs.values(), "SonicHostFilesystem", privileged = True)
    return [
        DefaultInfo(files = depset(outputs.values())),
        HostInfo(fs = outputs["fs"], boot = outputs["boot"], platform = outputs["platform"]),
        OutputGroupInfo(**{name: depset([output]) for name, output in outputs.items()}),
    ]

host_filesystem = rule(
    implementation = _host_impl,
    attrs = {
        "source": attr.label(allow_single_file = True, mandatory = True),
        "snapshot": attr.label(allow_single_file = True, mandatory = True),
        "config": attr.label(allow_single_file = True, mandatory = True),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "metadata": attr.label_keyed_string_dict(mandatory = True, allow_files = True),
        "local_images": attr.label_keyed_string_dict(mandatory = True, allow_files = True),
        "build_script": attr.label(default = Label("//:build_debian.sh"), allow_single_file = True),
        "extension_template": attr.label(default = Label("//:files/build_templates/sonic_debian_extension.j2"), allow_single_file = True),
        "_script": attr.label(default = Label(":host.py"), allow_single_file = True),
        "_metadata": attr.label(default = Label(":metadata.py"), allow_single_file = True),
    },
)

def _payload_impl(ctx):
    host = ctx.attr.host[HostInfo]
    output = ctx.actions.declare_file(ctx.label.name + ".zip")
    args = [
        "payload",
        "--squashfs",
        host.fs.path,
        "--boot-tar",
        host.boot.path,
        "--platform-tar",
        host.platform.path,
        "--dockerfs",
        ctx.file.store.path,
        "--epoch",
        str(ctx.attr.epoch),
        "--output",
        output.path,
    ]
    _python_action(ctx, ctx.file._script, args, [host.fs, host.boot, host.platform, ctx.file.store], [output], "SonicImagePayload")
    return [DefaultInfo(files = depset([output]))]

image_payload = rule(
    implementation = _payload_impl,
    attrs = {
        "host": attr.label(providers = [HostInfo], mandatory = True),
        "store": attr.label(allow_single_file = True, mandatory = True),
        "epoch": attr.int(default = 0),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":installer.py"), allow_single_file = True),
    },
)

def _onie_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name)
    manifest = ctx.actions.declare_file(ctx.label.name + ".files.json")
    inputs = [ctx.file.payload, ctx.file.store, ctx.file.config, manifest]
    records = []
    for target, name in ctx.attr.installer_files.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("installer input must contain exactly one file")
        inputs += files
        records.append({
            "path": name,
            "source": files[0].path,
            "mode": int(ctx.attr.installer_modes.get(name, "493" if name.endswith(".sh") or name.endswith(".py") else "420")),
        })
    ctx.actions.write(manifest, json.encode(records))
    _python_action(
        ctx,
        ctx.file._script,
        [
            "onie",
            "--payload",
            ctx.file.payload.path,
            "--dockerfs",
            ctx.file.store.path,
            "--config",
            ctx.file.config.path,
            "--files",
            manifest.path,
            "--output",
            output.path,
        ],
        inputs,
        [output],
        "SonicOnieInstaller",
    )
    return [DefaultInfo(files = depset([output]))]

onie_installer = rule(
    implementation = _onie_impl,
    attrs = {
        "payload": attr.label(allow_single_file = True, mandatory = True),
        "store": attr.label(allow_single_file = True, mandatory = True),
        "config": attr.label(allow_single_file = True, mandatory = True),
        "installer_files": attr.label_keyed_string_dict(mandatory = True, allow_files = True),
        "installer_modes": attr.string_dict(),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":installer.py"), allow_single_file = True),
    },
)

def sonic_vs_image(
        name,
        images,
        local_images,
        source,
        snapshot,
        host_config,
        execution_environment,
        installer_config,
        installer_files,
        image_version,
        epoch = 0,
        installer_modes = {}):
    """Assemble a VS ONIE installer from declared native predecessors.

    Args:
        name: Final installer target and filename.
        images: Mapping of service archive basenames to input labels.
        local_images: Archive basenames whose full contents affect the host.
        source: Frozen native source bundle label.
        snapshot: Pre-container host SquashFS label.
        host_config: Host finalization configuration label.
        execution_environment: Pinned local worker identity label.
        installer_config: ONIE configuration label.
        installer_files: Mapping of installer input labels to payload paths.
        image_version: Version tag added to each Docker image.
        epoch: Reproducible ZIP timestamp in Unix seconds.
        installer_modes: Optional payload path to decimal mode mapping.
    """
    if images.get("docker-orchagent.gz") != "//dockers/docker-orchagent:docker-orchagent.gz":
        fail("the VS image must use the source-built SWSS archive and matching OCI metadata")
    parts = []
    metadata = {}
    for archive, src in images.items():
        image = archive.removesuffix(".gz")
        part = name + "_" + image + "_store"
        docker_store_part(
            name = part,
            src = src,
            execution_environment = execution_environment,
            tags_to_add = [image + ":latest=" + image + ":" + image_version],
        )
        parts.append(":" + part)
        if archive not in local_images:
            projection = name + "_" + image + "_metadata"
            service_metadata(
                name = projection,
                src = "//dockers/docker-orchagent:docker-orchagent" if archive == "docker-orchagent.gz" else src,
                oci = archive == "docker-orchagent.gz",
                execution_environment = execution_environment,
            )
            metadata[":" + projection] = archive
    host_filesystem(
        name = name + "_host",
        source = source,
        snapshot = snapshot,
        config = host_config,
        metadata = metadata,
        local_images = {images[archive]: archive for archive in local_images},
        execution_environment = execution_environment,
    )
    docker_store(name = name + "_dockerfs", parts = parts, execution_environment = execution_environment)
    image_payload(name = name + "_fs", host = ":" + name + "_host", store = ":" + name + "_dockerfs", epoch = epoch, execution_environment = execution_environment)
    onie_installer(
        name = name,
        payload = ":" + name + "_fs",
        store = ":" + name + "_dockerfs",
        config = installer_config,
        installer_files = installer_files,
        installer_modes = installer_modes,
        execution_environment = execution_environment,
    )
