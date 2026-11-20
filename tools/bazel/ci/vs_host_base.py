#!/usr/bin/python3
"""One cold capture and a verified cross-worker Bazel disk-cache restore of a VS host base."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

IMAGE = Path("target/sonic-vs.bin__vs__rfs.squashfs")
INVENTORY = Path("target/versions/host-base-image/versions-deb-trixie-amd64")
RECEIPTS = Path("artifacts/vs/hostbase-experiment")
PRIVATE_CACHE = Path("/dpkg_cache")
BAZEL_DIGEST = "61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e"
TOOL = Path(__file__).resolve()
FIELDS = """
TARGET_PATH TARGET_MACHINE TARGET_BOOTLOADER IMAGE_TYPE IMAGE_DISTRO CONFIGURED_PLATFORM
CONFIGURED_ARCH BLDENV RFS_SQUASHFS_NAME RFS_SPLIT_FIRST_STAGE RFS_SPLIT_LAST_STAGE
SONIC_VS_UNPERSONALIZED_BASE USERNAME debs_path kversion linux_kernel initramfs_tools
sonicadmin_user sonic_asic_platform image_type INCLUDE_KUBERNETES INCLUDE_KUBERNETES_MASTER
KUBERNETES_VERSION KUBERNETES_CRI_TOOLS_VERSION KUBERNETES_CNI_VERSION MASTER_KUBERNETES_VERSION
MASTER_CRI_DOCKERD MULTIARCH_QEMU_ENVIRON CROSS_BUILD_ENVIRON ENABLE_VERSION_CONTROL_DEB
MIRROR_SNAPSHOT BUILD_PUBLIC_URL BUILD_SNAPSHOT_URL TRUSTED_GPG_URLS BUILD_PACKAGES_URL
MIRROR_URLS MIRROR_SECURITY_URLS SONIC_VERSION_CONTROL_COMPONENTS SONIC_VERSION_CACHE ENABLE_SBOM
""".split()
URL_FIELDS = "BUILD_PUBLIC_URL BUILD_SNAPSHOT_URL TRUSTED_GPG_URLS BUILD_PACKAGES_URL MIRROR_URLS MIRROR_SECURITY_URLS".split()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def trial_id(value):
    check(bool(re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", value)), "invalid trial ID")
    return value


def data_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def fingerprint(path):
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            sha.update(block)
            size += len(block)
    return {"sha256": sha.hexdigest(), "bytes": size}


def private_dir(path):
    info = path.lstat()
    check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
          f"trial directory must be private and owned by the builder: {path}")
    check(path.resolve(strict=True) == path, f"trial directory traverses a symlink: {path}")


def namespace(parent, name, create):
    trial = parent / (".vs-hostbase-trial-" + trial_id(name))
    marker = trial / "trial.json"
    manifest = {"format": 1, "trial_id": name, "purpose": "sonic-vs-host-base"}
    if create:
        trial.mkdir(mode=0o700)
        private_dir(trial)
        (trial / "cache").mkdir(mode=0o700)
        marker.write_bytes(data_bytes(manifest))
    private_dir(trial)
    private_dir(trial / "cache")
    check(not marker.is_symlink() and marker.read_bytes() == data_bytes(manifest), "wrong trial namespace")
    return trial


def cleanup(name):
    trial = namespace(PRIVATE_CACHE, name, False)
    check({p.name for p in trial.iterdir()} == {"cache", "trial.json"}, "unexpected namespace contents")
    shutil.rmtree(trial)


def bazel_binary():
    path = Path(f"downloads/sha256/{BAZEL_DIGEST}/bin/bazel")
    roots = [Path(os.getenv("BAZELISK_HOME", Path.home() / ".cache/bazelisk")), Path("/bazel_cache/bazelisk")]
    candidates = [p / path for p in roots]
    candidates += [Path(p) for p in (shutil.which("bazel-real"), shutil.which("bazel")) if p]
    for binary in candidates:
        if binary.is_file() and fingerprint(binary)["sha256"] == BAZEL_DIGEST:
            return binary.resolve()
    raise ValueError("pinned native Bazel 8.5.1 is unavailable in this build slave")


def allowed_env():
    cfg = {key: os.environ[key] for key in FIELDS if key in os.environ}
    expected = {"TARGET_PATH": "target", "TARGET_MACHINE": "vs", "TARGET_BOOTLOADER": "grub",
                "IMAGE_DISTRO": "trixie", "CONFIGURED_ARCH": "amd64", "USERNAME": "admin",
                "RFS_SPLIT_FIRST_STAGE": "y", "RFS_SPLIT_LAST_STAGE": "n",
                "RFS_SQUASHFS_NAME": IMAGE.name, "SONIC_VS_UNPERSONALIZED_BASE": "y"}
    for k, v in expected.items():
        check(cfg.get(k) == v, f"unsupported trial setting {k}")
    check(cfg.get("SONIC_VERSION_CACHE", "") in ("", "none"), "trial requires the stock disabled version cache")
    for k in URL_FIELDS:
        for item in cfg.get(k, "").split():
            url = urlsplit(item)
            check(url.username is None and url.password is None and not url.query and not url.fragment,
                  f"credentialed build URL is not permitted in {k}")
    return cfg


def inputs(repo, cfg, name, builder):
    check(re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", builder), "builder must be an immutable SHA-256 image ID")
    options = dict(cfg)
    for field in ("debs_path", "linux_kernel", "initramfs_tools"):
        options[field] = (repo / cfg[field]).resolve().relative_to(repo).as_posix()
    records = {}

    def add(path):
        actual = path.resolve(strict=True)
        actual.relative_to(repo)
        key = path.relative_to(repo).as_posix()
        if actual.is_dir():
            for child in sorted(path.iterdir()):
                add(child)
        elif actual.is_file():
            records[key] = {"sha256": fingerprint(actual)["sha256"], "mode": stat.S_IMODE(actual.stat().st_mode) & 0o111}
            if path.is_symlink():
                records[key]["target"] = os.readlink(path)
        else:
            raise ValueError(f"unsupported input {key}")

    sources = [
        "build_debian.sh", "functions.sh", "onie-image.conf", ".platform", ".arch",
        "scripts/vs_host_base_state.sh", "scripts/build_debian_base_system.sh",
        "scripts/prepare_debian_image_buildinfo.sh", "scripts/build_mirror_config.sh", "scripts/versions_manager.py",
        "files/initramfs-tools", "files/apt/apt.conf.d", "files/apt/sources.list.j2", "files/apt/apt-retries-count.j2",
        "files/apparmor/usr.bin.tcpdump", "files/docker/docker.service.conf", "files/sshd",
        "files/image_config/kdump/vmcore-sysctl.conf", "files/image_config/logrotate/logrotateOverride.conf",
        "files/image_config/sysctl/90-sonic.conf", "files/image_config/pip/pip.conf", "files/dhcp",
        "files/scripts/core_cleanup.py", "files/build_scripts/generate_asic_config_checksum.py",
        "files/build_templates/sed_config.conf.j2", "files/build/versions/default", "files/build/versions/host-image",
        "files/build/versions/host-base-image", "src/sonic-build-hooks/buildinfo",
        "src/sonic-swss/swssconfig/sample/netbouncer.json",
    ]
    if cfg.get("MIRROR_SNAPSHOT") == "y":
        sources.append("target/versions/default/versions-mirror")
    for optional in ["platform/vs/modules", "files/apt/sources.list.amd64.j2"]:
        if (repo / optional).exists():
            sources.append(optional)
    for source in sources:
        add(repo / source)
    for generated in ["files/initramfs-tools/arista-convertfs", "files/initramfs-tools/union-mount",
                      "src/sonic-build-hooks/buildinfo/sonic-build-hooks_1.0_all.deb"]:
        check(generated in records, f"missing generated input {generated}")
    for pattern in ["initramfs-tools-core_*.deb", "initramfs-tools_*.deb", "linux-image-6.12.41+deb13-*_amd64.deb"]:
        selected = [p for p in (repo / options["debs_path"]).glob(pattern) if "-dbg_" not in p.name and "-dbgsym_" not in p.name]
        check(selected, f"no selected {pattern} input")
        for path in selected:
            add(path)
    git = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    check(re.fullmatch(r"[0-9a-f]{40}", revision), "expected a pinned Git checkout")
    return {"format": 1, "trial_id": name, "builder": builder, "revision": revision, "options": options,
            "inputs": records, "driver": fingerprint(TOOL), "rule": fingerprint(TOOL.with_suffix(".bzl"))}


def control_dir(name):
    return Path("/tmp") / ("sonic-vs-host-base-" + trial_id(name))


def native_error(logfile, repo):
    locations = []
    reasons = []
    with logfile.open(errors="replace") as stream:
        for entry in stream:
            line = entry.rstrip()
            failure = re.fullmatch(r"__VS_BASE_ERROR__:(?:\./)?(build_debian\.sh|scripts/[a-zA-Z0-9_.-]+\.sh):(\d{1,6}):(\d{1,3})", line)
            if failure:
                script, lineno, status = failure.groups()
                source = (repo / script).read_text().splitlines()
                if 1 <= int(lineno) <= len(source):
                    # Show the public source statement, never its expanded credential-bearing shell arguments.
                    locations.append(f"{script}:{lineno} (exit {status}) {source[int(lineno) - 1].strip()[:180]}")
            if re.fullmatch(r"VS host base: [a-zA-Z0-9 _/.:+-]{1,150}|E: Unable to locate package [a-z0-9.+-]+", line):
                reasons.append(line)
    return " | ".join(dict.fromkeys((reasons + locations)[-8:])) or "see private target/vs-host-base-native.log"


def producer(identity_path, image_output, inventory_output):
    identity = json.loads(identity_path.read_bytes())
    control = control_dir(identity["trial_id"])
    private_dir(control)
    permit = control / "permit.json"
    check(permit.is_file(), "cache miss on the restore worker: native build disabled")
    consumed = control / "consumed.json"
    permit.rename(consumed)
    job = json.loads(consumed.read_bytes())
    check(job["identity_sha256"] == fingerprint(identity_path)["sha256"], "wrong native build permit")
    repo = Path(job["repo"])
    env = job["env"]
    image = repo / IMAGE
    inventory = repo / INVENTORY
    check(not image.exists(), "native image already exists")
    # Only unexpanded source locations are reported to Actions if the native producer fails.
    tracing = control / "native-error-trap.sh"
    tracing.write_text("""set -E
trap 'printf "__VS_BASE_ERROR__:%s:%s:%s\\n" "${BASH_SOURCE[0]}" "$LINENO" "$?" >&2' ERR
""")
    env["BASH_ENV"] = str(tracing)
    start = time.monotonic()
    logfile = repo / "target/vs-host-base-native.log"
    with logfile.open("wb") as log:
        proc = subprocess.run(["./build_debian.sh"], cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode:
        raise ValueError(f"native stage exited {proc.returncode}: {native_error(logfile, repo)}")
    seconds = time.monotonic() - start
    subprocess.run(["/usr/bin/unsquashfs", "-processors", "1", "-cat", image, "etc/sonic/.vs-host-base-v1"],
                   stdout=subprocess.DEVNULL, check=True)
    check(inventory.stat().st_size > 0, "native package inventory missing")
    shutil.copyfile(image, image_output)
    shutil.copyfile(inventory, inventory_output)
    (control / "executed.json").write_bytes(data_bytes({"native_build_seconds": seconds}))


def build(work, cache, identity, binary):
    ws = work / "workspace"
    ws.mkdir()
    (ws / "WORKSPACE").write_text('workspace(name = "sonic_vs_host_base")\n')
    (ws / "resolved.bzl").write_text("resolved = []\n")
    (ws / "identity.json").write_bytes(data_bytes(identity))
    shutil.copyfile(TOOL, ws / "driver.py")
    (ws / "driver.py").chmod(0o755)
    shutil.copyfile(TOOL.with_suffix(".bzl"), ws / "host.bzl")
    (ws / "BUILD").write_text(
        'load(":host.bzl", "vs_host_base")\nplatform(name = "native")\n'
        'vs_host_base(name = "base", driver = ":driver.py", identity = ":identity.json")\n')
    # No system/user/workspace rc or network cache. Every invocation gets an empty local output base.
    log = work / "actions.json"
    args = [str(binary), "--batch", "--ignore_all_rc_files", f"--output_user_root={work}/user",
            f"--output_base={work}/base", "--host_jvm_args=-XX:ActiveProcessorCount=2", "build",
            "--jobs=1", "--noenable_bzlmod", "--enable_workspace", "--incompatible_autoload_externally=",
            f"--experimental_resolved_file_instead_of_workspace={ws}/resolved.bzl", "--repository_disable_download",
            "--platforms=//:native", "--host_platform=//:native", "--spawn_strategy=local", "--bes_backend=",
            "--remote_cache=", "--remote_executor=", f"--disk_cache={cache}",
            f"--execution_log_json_file={log}", "//:base"]
    subprocess.run(args, cwd=ws, check=True, env={"HOME": str(work), "PATH": "/usr/bin:/bin", "LANG": "C"})
    action = json.loads(log.read_bytes())
    check(isinstance(action, dict) and action.get("mnemonic") == "SonicVsHostBase", "missing host-base action evidence")
    return action, ws / "bazel-bin/host.squashfs", ws / "bazel-bin/host.inventory"


def put_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data_bytes(value))
    os.replace(f.name, path)


def restore_pair(repo, outputs, expected):
    # Verify both private staged files before publishing the Make target.
    staged = {}
    try:
        for name, (src, dst) in outputs.items():
            destination = repo / dst
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as out:
                staged[name] = Path(out.name)
                with src.open("rb") as stream:
                    shutil.copyfileobj(stream, out, 1024 * 1024)
            check(fingerprint(staged[name]) == expected[name], f"cached {name} differs from trusted cold receipt")
            staged[name].chmod(0o644)
        for name in ("inventory", "image"):
            os.replace(staged[name], repo / outputs[name][1])
    finally:
        for p in staged.values():
            p.unlink(missing_ok=True)


def experiment():
    begin = time.monotonic()
    repo = Path.cwd().resolve()
    mode = os.environ["SONIC_HOSTBASE_MODE"]
    name = trial_id(os.environ["SONIC_HOSTBASE_TRIAL_ID"])
    worker = os.environ["SONIC_HOSTBASE_WORKER_ID"]
    check(mode in ("cold", "warm") and re.fullmatch(r"[a-zA-Z0-9._-]{1,160}", worker), "invalid worker or trial mode")
    subprocess.run(["bash", "scripts/vs_host_base_state.sh", "check-inputs"], check=True)
    cfg = allowed_env()
    identity = inputs(repo, cfg, name, os.environ["SONIC_HOSTBASE_BUILDER_ID"])
    key = hashlib.sha256(data_bytes(identity)).hexdigest()
    out = repo / RECEIPTS
    out.mkdir(parents=True, exist_ok=True)
    check(not (repo / IMAGE).exists(), "the Make target must be absent at trial entry")
    receipt = {"trial_id": name, "worker_id": worker, "mode": mode, "identity_sha256": key,
               "revision": identity["revision"], "builder": identity["builder"]}
    # Per-input digests in the trusted receipt allow inspecting a mismatch without disclosing contents.
    field_digests = {f"file:{k}": hashlib.sha256(data_bytes(v)).hexdigest() for k, v in identity["inputs"].items()}
    field_digests.update({f"option:{k}": hashlib.sha256(data_bytes(v)).hexdigest() for k, v in identity["options"].items()})
    for k in ("revision", "builder", "driver", "rule"):
        field_digests[k] = hashlib.sha256(data_bytes(identity[k])).hexdigest()
    cold = mode == "cold"
    if not cold:
        trusted = json.loads((out / "cold-receipt.json").read_bytes())
        if trusted["identity_sha256"] != key:
            previous = trusted["input_digests"]
            differences = sorted(k for k in (previous.keys() | field_digests.keys()) if previous.get(k) != field_digests.get(k))
            raise ValueError("cold/warm input identity differs: " + ", ".join(differences[:30]))
        check(trusted["trial_id"] == name and trusted["mode"] == "cold"
              and trusted["worker_id"] != worker and trusted["cache_hit"] is False,
              "wrong input identity, trial, or worker for trusted cold receipt")
    binary = bazel_binary()
    namespace_dir = namespace(PRIVATE_CACHE, name, cold)
    control = control_dir(name)
    control.mkdir(mode=0o700)
    try:
        private_dir(control)
        if cold:
            account = pwd.getpwuid(os.geteuid())
            env = dict(cfg, USER=account.pw_name, LOGNAME=account.pw_name, HOME=account.pw_dir,
                       LANG="C", PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            (control / "permit.json").write_bytes(data_bytes({"identity_sha256": key, "repo": str(repo), "env": env}))
        with tempfile.TemporaryDirectory(prefix="vs-hostbase-bazel-", dir="/tmp") as tmp:
            before = time.monotonic()
            event, image, inventory = build(Path(tmp), namespace_dir / "cache", identity, binary)
            receipt["bazel_invocation_seconds"] = time.monotonic() - before
            hit = event.get("cacheHit", False)
            runner = event.get("runner", "")
            check(hit == (not cold) and runner == ("local" if cold else "disk cache hit"), "unexpected Bazel executor/cache outcome")
            executed = control / "executed.json"
            check(executed.exists() == cold, "unexpected local producer execution")
            receipt.update(cache_hit=hit, runner=runner, mnemonic=event["mnemonic"], native_build_seconds=0)
            files = {"image": (image, IMAGE), "inventory": (inventory, INVENTORY)}
            if cold:
                receipt.update(json.loads(executed.read_bytes()))
                receipt["outputs"] = {k: fingerprint(src) for k, (src, _) in files.items()}
                for k, (_, dest) in files.items():
                    check(fingerprint(repo / dest) == receipt["outputs"][k], "native output differs from Bazel output")
            else:
                receipt["outputs"] = trusted["outputs"]
                restore_pair(repo, files, receipt["outputs"])
            receipt["verify_and_materialize_seconds"] = time.monotonic() - before - receipt["bazel_invocation_seconds"]
    except BaseException:
        # Make must never reuse a target from a failed experiment.
        if cold and (repo / IMAGE).is_file():
            (repo / IMAGE).unlink()
        raise
    finally:
        shutil.rmtree(control)
    receipt["elapsed_seconds"] = time.monotonic() - begin
    put_json(out / (mode + "-receipt.json"), dict(receipt, input_digests=field_digests))
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--", "./build_debian.sh"]:
        experiment()
    elif len(sys.argv) == 5 and sys.argv[1] == "--action":
        producer(*(Path(p) for p in sys.argv[2:]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--cleanup-trial":
        cleanup(sys.argv[2])
    else:
        sys.exit("use -- ./build_debian.sh or --cleanup-trial RUN-ATTEMPT")
