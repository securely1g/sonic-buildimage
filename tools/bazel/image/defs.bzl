"""Cacheable SONiC VS image assembly from explicit phase-one native inputs.

The default keeps the classic Docker/overlay2 store. The opt-in containerd
backend converts service archives with Skopeo and imports complete content
without unpacking snapshots. Both paths use isolated privileged workers and
metadata projections to keep binary edits off the host dependency edge.
"""

ContainerdStoreInfo = provider("Stopped containerd store and verified inventory.", fields = ["archive", "receipt"])

HostInfo = provider("Finalized host filesystem and boot payloads.", fields = ["fs", "boot", "platform"])

def _host_resources(_os, _input_size):
    return {"cpu": 4, "memory": 2048}

def _python_action(ctx, script, args, inputs, outputs, mnemonic, privileged = False, readonly_output = False):
    command = 'cmp -- "$1" /run/sonic-image-worker.json && shift && exec /usr/bin/python3 "$@"'
    arguments = [ctx.file.execution_environment.path, script.path] + args
    if readonly_output:
        if len(outputs) != 1:
            fail("readonly_output requires one declared output file")

        # Bazel's default output mode is 0555. Set it before spawn completion:
        # otherwise execution logging hashes the large archive, then Bazel's
        # chmod changes ctime and invalidates that digest before output checks.
        command = 'cmp -- "$1" /run/sonic-image-worker.json && image_output="$2" && shift 2 && /usr/bin/python3 "$@" && chmod 0555 -- "$image_output"'
        arguments = [ctx.file.execution_environment.path, outputs[0].path, script.path] + args
    ctx.actions.run_shell(
        command = command,
        arguments = arguments,
        inputs = depset(inputs + [script, ctx.file.execution_environment]),
        outputs = outputs,
        mnemonic = mnemonic,
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC"},
        execution_requirements = {"no-sandbox": "1", "no-remote-exec": "1"} if privileged else {"no-remote-exec": "1"},
        resource_set = _host_resources if privileged else None,
    )

def _metadata_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    _python_action(
        ctx,
        ctx.file._script,
        ["--archive", ctx.file.src.path, "--output", output.path],
        [ctx.file.src],
        [output],
        "SonicServiceMetadata",
    )
    return [DefaultInfo(files = depset([output]))]

service_metadata = rule(
    implementation = _metadata_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
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
        resource_set = _host_resources,
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
    _python_action(ctx, ctx.file._script, args, ctx.files.parts, [output], "SonicDockerStore", readonly_output = True)
    return [DefaultInfo(files = depset([output]))]

docker_store = rule(
    implementation = _merge_impl,
    attrs = {
        "parts": attr.label_list(allow_files = True, mandatory = True),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":store.py"), allow_single_file = True),
    },
)

def _archive_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name + ".oci")
    _python_action(
        ctx,
        ctx.file._script,
        [
            "convert",
            "--source",
            ctx.file.src.path,
            "--source-format",
            ctx.attr.source_format,
            "--name",
            ctx.attr.image_name,
            "--architecture",
            ctx.attr.architecture,
            "--version",
            ctx.attr.image_version,
            "--output",
            output.path,
        ],
        [ctx.file.src],
        [output],
        "SonicOciArchive",
    )
    return [DefaultInfo(files = depset([output]))]

oci_archive = rule(
    implementation = _archive_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "source_format": attr.string(values = ["docker-archive", "oci", "oci-archive"], mandatory = True),
        "image_name": attr.string(mandatory = True),
        "image_version": attr.string(mandatory = True),
        "architecture": attr.string(default = "amd64", values = ["amd64", "arm64"]),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":oci_bundle.py"), allow_single_file = True),
    },
)

def _containerd_store_impl(ctx):
    archive = ctx.actions.declare_file(ctx.label.name + ".tar.gz")
    receipt = ctx.actions.declare_file(ctx.label.name + ".receipt.json")
    args = ctx.actions.args()
    args.add(ctx.file._script)
    args.add("--output", archive)
    args.add("--receipt", receipt)
    args.add("--execution-environment", ctx.file.execution_environment)
    args.add("--jobs", ctx.attr.jobs)
    for part in ctx.files.parts:
        args.add("--part", part.path)
    inputs = ctx.files.parts + ctx.files._support + [ctx.file._script, ctx.file.execution_environment]
    if ctx.attr.base:
        base = ctx.attr.base[ContainerdStoreInfo]
        args.add("--base", base.archive)
        args.add("--base-receipt", base.receipt)
        inputs += [base.archive, base.receipt]
    ctx.actions.run_shell(
        command = 'exec sudo -n unshare --mount --pid --fork --kill-child --mount-proc --net /usr/bin/python3 "$@" --uid "$(id -u)" --gid "$(id -g)"',
        arguments = [args],
        inputs = depset(inputs),
        outputs = [archive, receipt],
        mnemonic = "SonicContainerdStore",
        resource_set = _host_resources,
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC"},
        execution_requirements = {"no-sandbox": "1", "no-remote-exec": "1"},
    )
    return [
        DefaultInfo(files = depset([archive])),
        ContainerdStoreInfo(archive = archive, receipt = receipt),
        OutputGroupInfo(receipt = depset([receipt])),
    ]

containerd_store = rule(
    implementation = _containerd_store_impl,
    attrs = {
        "parts": attr.label_list(allow_files = True, mandatory = True),
        "base": attr.label(providers = [ContainerdStoreInfo]),
        "jobs": attr.int(default = 4),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "_script": attr.label(default = Label(":containerd_store.py"), allow_single_file = True),
        "_support": attr.label_list(default = [Label(":oci_bundle.py"), Label(":containerd_import.py")], allow_files = True),
    },
)

def _host_impl(ctx):
    outputs = {name: ctx.actions.declare_file(ctx.label.name + "." + suffix) for name, suffix in {
        "fs": "squashfs",
        "boot": "boot.tar",
        "platform": "platform.tar.gz",
        "receipt": "receipt.json",
    }.items()}
    args = ["--image-store-backend", "containerd" if ctx.attr.use_container_image_store_as_backend else "docker"]
    inputs = [ctx.file._metadata]
    files = {
        "source": ctx.file.source,
        "snapshot": ctx.file.snapshot,
        "config": ctx.file.config,
        "execution-environment": ctx.file.execution_environment,
        "build-script": ctx.file.build_script,
        "extension-template": ctx.file.extension_template,
    }
    if ctx.attr.use_container_image_store_as_backend:
        files.update({"runtime-overlay": ctx.file._runtime_overlay, "offline-host": ctx.file._offline_host})
        inputs += ctx.files._runtime_files
    for flag, file in files.items():
        args += ["--" + flag, file.path]
        inputs.append(file)
    for flag, targets in [("metadata", ctx.attr.metadata), ("local-image", ctx.attr.local_images)]:
        for target, name in targets.items():
            image_files = target[DefaultInfo].files.to_list()
            if len(image_files) != 1:
                fail("host image inputs must contain exactly one file")
            args += ["--" + flag, name + "=" + image_files[0].path]
            inputs += image_files
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
        "use_container_image_store_as_backend": attr.bool(default = False),
        "source": attr.label(allow_single_file = True, mandatory = True),
        "snapshot": attr.label(allow_single_file = True, mandatory = True),
        "config": attr.label(allow_single_file = True, mandatory = True),
        "execution_environment": attr.label(allow_single_file = True, mandatory = True),
        "metadata": attr.label_keyed_string_dict(mandatory = True, allow_files = True),
        "local_images": attr.label_keyed_string_dict(mandatory = True, allow_files = True),
        "build_script": attr.label(default = Label("//:build_debian.sh"), allow_single_file = True),
        "extension_template": attr.label(default = Label("//files/build_templates:sonic_debian_extension.j2"), allow_single_file = True),
        "_script": attr.label(default = Label(":host.py"), allow_single_file = True),
        "_metadata": attr.label(default = Label(":metadata.py"), allow_single_file = True),
        "_offline_host": attr.label(default = Label(":offline_host.py"), allow_single_file = True),
        "_runtime_overlay": attr.label(default = Label("//tools/bazel/image/containerd_runtime:overlay.py"), allow_single_file = True),
        "_runtime_files": attr.label(default = Label("//tools/bazel/image/containerd_runtime:files")),
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
    _python_action(ctx, ctx.file._script, args, [host.fs, host.boot, host.platform, ctx.file.store], [output], "SonicImagePayload", readonly_output = True)
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
    extra_args = []
    if ctx.attr.use_container_image_store_as_backend:
        inputs.append(ctx.file._dockerfs_extractor)
        extra_args = ["--dockerfs-extractor", ctx.file._dockerfs_extractor.path]
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
        ] + extra_args,
        inputs,
        [output],
        "SonicOnieInstaller",
        readonly_output = True,
    )
    return [DefaultInfo(files = depset([output]))]

onie_installer = rule(
    implementation = _onie_impl,
    attrs = {
        "use_container_image_store_as_backend": attr.bool(default = False),
        "_dockerfs_extractor": attr.label(default = Label("//tools/bazel/image/containerd_runtime:extract_store.sh"), allow_single_file = True),
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
        installer_modes = {},
        use_container_image_store_as_backend = False):
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
        use_container_image_store_as_backend: Select containerd instead of the default Docker store.
    """
    if type(use_container_image_store_as_backend) != "bool":
        fail("use_container_image_store_as_backend must be a boolean")
    if use_container_image_store_as_backend and not images:
        fail("the containerd store requires at least one service archive")
    base_parts = []
    docker_parts = []
    swss_part = None
    local_parts = {}
    metadata = {}
    for archive, src in images.items():
        image = archive.removesuffix(".gz")
        if use_container_image_store_as_backend:
            part = name + "_" + image + "_oci"
            oci_archive(
                name = part,
                src = src,
                source_format = "docker-archive",
                image_name = image,
                image_version = image_version,
                execution_environment = execution_environment,
            )
            if archive == "docker-orchagent.gz":
                swss_part = ":" + part
            else:
                base_parts.append(":" + part)
            if archive in local_images:
                local_parts[":" + part] = archive
        else:
            part = name + "_" + image + "_store"
            docker_store_part(
                name = part,
                src = src,
                execution_environment = execution_environment,
                tags_to_add = [image + ":latest=" + image + ":" + image_version],
            )
            docker_parts.append(":" + part)
            if archive in local_images:
                local_parts[src] = archive
        if archive not in local_images:
            projection = name + "_" + image + "_metadata"
            service_metadata(
                name = projection,
                src = src,
                execution_environment = execution_environment,
            )
            metadata[":" + projection] = archive
    host_filesystem(
        name = name + "_host",
        source = source,
        snapshot = snapshot,
        config = host_config,
        metadata = metadata,
        local_images = local_parts,
        execution_environment = execution_environment,
        use_container_image_store_as_backend = use_container_image_store_as_backend,
    )
    if use_container_image_store_as_backend:
        stages = ["dockerfs"]
        if swss_part and base_parts:
            # Orchagent edits reuse the stopped store containing the other
            # service archives. This is a cache boundary, not a source build.
            containerd_store(
                name = name + "_containerd_base",
                parts = base_parts,
                execution_environment = execution_environment,
            )
            containerd_store(
                name = name + "_dockerfs",
                parts = [swss_part],
                base = ":" + name + "_containerd_base",
                execution_environment = execution_environment,
            )
            stages.append("containerd_base")
        else:
            containerd_store(
                name = name + "_dockerfs",
                parts = base_parts + ([swss_part] if swss_part else []),
                execution_environment = execution_environment,
            )
        for stage in stages:
            native.filegroup(
                name = name + "_" + stage + "_receipt",
                srcs = [":" + name + "_" + stage],
                output_group = "receipt",
            )
    else:
        docker_store(
            name = name + "_dockerfs",
            parts = docker_parts,
            execution_environment = execution_environment,
        )
    image_payload(name = name + "_fs", host = ":" + name + "_host", store = ":" + name + "_dockerfs", epoch = epoch, execution_environment = execution_environment)
    onie_installer(
        name = name,
        payload = ":" + name + "_fs",
        store = ":" + name + "_dockerfs",
        config = installer_config,
        installer_files = installer_files,
        installer_modes = installer_modes,
        execution_environment = execution_environment,
        use_container_image_store_as_backend = use_container_image_store_as_backend,
    )
