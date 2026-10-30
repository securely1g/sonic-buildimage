#!/usr/bin/env python3
"""Validate build-time containerd import and restoration with real tools.

Run in a disposable container or mount/PID/network namespace, with a fresh
--work-directory on an overlayfs-compatible filesystem. Import only while
building the archives, then start Docker against restored stores. The test
refuses an existing containerd socket and owns only the daemons it starts.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_bundle
import containerd_import as import_images
from containerd_runtime import overlay


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return stream.getvalue()


def entry(name, data=b"", mode=0o644, uid=0, gid=0):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.uid, info.gid = len(data), mode, uid, gid
    return info, data


def runtime_inputs(root, architecture):
    """Copy the worker's native shell/loader; no network or committed binary."""
    shell = Path(shutil.which("sh"))
    linked = subprocess.check_output(["ldd", str(shell)], text=True)
    libraries = {Path(value) for value in re.findall(r"(/[^\s()]+)", linked)}
    entries = [entry("bin/sh", shell.read_bytes(), 0o755)]
    entries += [entry(str(path).lstrip("/"), path.read_bytes(), 0o755) for path in sorted(libraries)]
    entries += [entry("probe", b"content-preserved\n", 0o600, 100, 101),
                entry("group-probe", b"group-preserved\n", 0o040, 0, 101),
                entry("removed", b"must disappear"), entry("opaque/old", b"must disappear")]
    link = tarfile.TarInfo("hardlink")
    link.type, link.linkname = tarfile.LNKTYPE, "probe"
    entries.append((link, None))
    layers = [tar_bytes(entries), tar_bytes([entry(".wh.removed"),
              entry("opaque/.wh..wh..opq"), entry("opaque/new", b"visible")])]
    command = ("set -eu; test \"$SONIC_PROBE\" = preserved; read value < /probe; test \"$value\" = content-preserved; "
               "read value < /group-probe; test \"$value\" = group-preserved; "
               "test /probe -ef /hardlink; test ! -e /removed; test ! -e /opaque/old; "
               "test -f /opaque/new; printf 'SONIC_CONTAINERD_OK\\n'")
    config = {"architecture": architecture, "os": "linux", "created": "2026-01-01T00:00:00Z",
              "config": {"User": "100:101", "Cmd": ["/bin/sh", "-c", command],
                         "Env": ["SONIC_PROBE=preserved"], "Labels": {"org.sonic.acceptance": "preserved"}},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(x).hexdigest() for x in layers]},
              "history": [{"created_by": "target import fixture"} for _ in layers]}
    config_bytes = oci_bundle.canonical(config)
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    blobs = {config_hash: config_bytes}
    descriptors = []
    for layer in layers:
        digest = hashlib.sha256(layer).hexdigest()
        blobs[digest] = layer
        descriptors.append({"mediaType": "application/vnd.oci.image.layer.v1.tar",
                            "digest": "sha256:" + digest, "size": len(layer)})
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": "sha256:" + config_hash, "size": len(config_bytes)},
                "layers": descriptors}
    manifest_bytes = oci_bundle.canonical(manifest)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    blobs[digest] = manifest_bytes
    layout = root / "input.oci"
    (layout / "blobs/sha256").mkdir(parents=True)
    for name, data in blobs.items():
        (layout / "blobs/sha256" / name).write_bytes(data)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')
    (layout / "index.json").write_bytes(oci_bundle.canonical({"schemaVersion": 2,
        "manifests": [{"mediaType": manifest["mediaType"], "digest": "sha256:" + digest,
                       "size": len(manifest_bytes)}]}))
    docker = root / "input.docker.tar.gz"
    # The changed service adds a real layer to the cached base chain, so the
    # final-stage test also exercises unpack after relocating a cached store.
    added_layer = tar_bytes([entry("changed", b"changed-service\n", 0o600, 100, 101)])
    docker_layers = layers + [added_layer]
    docker_config = json.loads(json.dumps(config))
    docker_config["rootfs"]["diff_ids"].append("sha256:" + hashlib.sha256(added_layer).hexdigest())
    docker_config["history"].append({"created_by": "changed service layer"})
    docker_config["config"]["Cmd"] = ["/bin/sh", "-c",
        'set -eu; read changed < /changed; test "$changed" = changed-service; ' + command]
    docker_config_bytes = oci_bundle.canonical(docker_config)
    docker_config_hash = hashlib.sha256(docker_config_bytes).hexdigest()
    names = [f"layer-{number}.tar" for number in range(len(docker_layers))]
    docker_manifest = [{"Config": docker_config_hash + ".json", "Layers": names,
                        "RepoTags": ["docker-archive-probe:latest", "docker-archive-probe:original"]}]
    with tarfile.open(docker, "w:gz") as archive:
        for info, data in [entry("manifest.json", oci_bundle.canonical(docker_manifest)),
                           entry(docker_config_hash + ".json", docker_config_bytes),
                           *[entry(name, data) for name, data in zip(names, docker_layers)]]:
            archive.addfile(info, io.BytesIO(data))
    return layout, docker, {"oci": config, "docker-archive": docker_config}


class Lab:
    def __init__(self, root):
        self.root, self.processes, self.logs = root, [], []
        self.ctr = import_images.Ctr()

    def run(self, command, check=True):
        start = time.monotonic()
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=300)
        with (self.root / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps({"argv": command, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "seconds": time.monotonic() - start}) + "\n")
        if check:
            result.check_returncode()
        return result

    def start(self, command, name):
        log = (self.root / (name + ".log")).open("w")
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        self.logs.append(log)
        self.processes.append(process)
        return process

    def wait_ready(self, process, command):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("private daemon exited during startup")
            if self.run(command, check=False).returncode == 0:
                return
            time.sleep(.1)
        raise RuntimeError("private daemon startup timed out")

    def containerd(self, label, data_root=None):
        config = self.root / "containerd.toml"
        config.write_text('version = 2\ndisabled_plugins = ["io.containerd.grpc.v1.cri"]\n')
        daemon = self.start(["containerd", "--config", str(config), "--root", str(data_root or self.root / (label + "-containerd")),
            "--state", str(self.root / (label + "-state")), "--address", import_images.ADDRESS], label + "-containerd")
        self.wait_ready(daemon, self.ctr.prefix + ["version"])

    def docker(self, *arguments):
        return self.run(["docker", "--host", "unix://" + str(self.root / "docker.sock"), *arguments]).stdout

    def start_docker(self, label):
        config = self.root / "daemon.json"
        config.write_text('{"features":{"containerd-snapshotter":true}}\n')
        daemon = self.start(["dockerd", "--config-file", str(config), "--containerd", import_images.ADDRESS,
            "--containerd-namespace", "moby", "--host", "unix://" + str(self.root / "docker.sock"),
            "--data-root", str(self.root / (label + "-docker")), "--exec-root", str(self.root / (label + "-exec")),
            "--pidfile", str(self.root / (label + ".pid")), "--bridge=none", "--iptables=false",
            "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"], label + "-docker")
        self.wait_ready(daemon, ["docker", "--host", "unix://" + str(self.root / "docker.sock"), "info"])
        info = json.loads(self.docker("info", "--format", "{{json .}}"))
        assert info["Driver"] == "overlayfs", info["Driver"]
        assert info["ServerVersion"] == "28.5.2", info["ServerVersion"]
        (self.root / (label + "-info.json")).write_text(json.dumps(info, indent=2))

    def stop(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()
        # In a plain Python-entrypoint worker this process is PID1 and adopts
        # completed containerd shims after the daemons have reaped their tasks.
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break


def host_fixture(root, lab):
    """A target SquashFS containing the same GNU tar used by the real installer."""
    filesystem = root / "target-host"
    for directory in ("tmp", "var/lib/docker", "usr/bin", "bin"):
        (filesystem / directory).mkdir(parents=True, exist_ok=True)
    for executable in ("tar", "gzip", "zstd", "sh"):
        path = Path(shutil.which(executable)).resolve()
        destination = filesystem / ("bin/sh" if executable == "sh" else "usr/bin/" + executable)
        shutil.copy2(path, destination)
        linked = lab.run(["ldd", str(path)]).stdout
        for name in re.findall(r"(/[^\s()]+)", linked):
            library = Path(name)
            target = filesystem / str(library).lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(library, target)
    squashfs = root / "target-host.squashfs"
    lab.run(["mksquashfs", str(filesystem), str(squashfs), "-noappend", "-all-root",
             "-no-progress", "-processors", "2"])
    return filesystem, squashfs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--worker-image", required=True, help="exact image ID used to launch this disposable worker")
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(import_images.ADDRESS).exists():
        raise SystemExit("requires a disposable privileged worker with no existing containerd socket")
    if not import_images.DIGEST.fullmatch(args.worker_image):
        raise SystemExit("worker-image must be an immutable sha256 image ID")
    root = args.work_directory.resolve()
    root.mkdir(parents=True, exist_ok=False)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if architecture is None:
        raise SystemExit("only native AMD64 and ARM64 workers are supported")
    lab = Lab(root)
    expected = {"schema": 1, "platform": "linux/" + architecture, "worker_image": args.worker_image,
                "docker_version": "28.5.2", "containerd_version": "1.7.28", "distribution": "trixie"}
    worker_marker = Path("/run/sonic-image-worker.json")
    # The controller supplies this container's actual image ID, just as run.py
    # does. A locally committed worker can contain a stale image-layer marker.
    worker_marker.write_bytes(oci_bundle.canonical(expected))
    specification = root / "execution-environment.json"
    specification.write_bytes(oci_bundle.canonical(expected))
    versions = {name: lab.run([name, "--version"]).stdout.strip()
                for name in ("skopeo", "containerd", "dockerd", "docker", "ctr")}
    if "v1.7.28" not in versions["containerd"] or "28.5.2" not in versions["dockerd"]:
        raise SystemExit("acceptance requires target containerd 1.7.28 and Docker 28.5.2")
    layout, docker_archive, expected_configs = runtime_inputs(root, architecture)
    parts, expected_by_tag = [], {}
    for source, transport, name in [(layout, "oci", "docker-oci-probe"),
                                    (docker_archive, "docker-archive", "docker-archive-probe")]:
        part = root / name
        record = oci_bundle.convert(source, transport, name, architecture, "sonic-version", part)
        expected_by_tag.update({tag: expected_configs[transport] for tag in record["tags"]})
        parts.append(part)
    producer = Path(__file__).with_name("containerd_store.py")
    report = {"passed": False, "architecture": architecture, "versions": versions,
              "worker_image": args.worker_image, "scope": "build-time store and installer extraction; no SONiC guest boot",
              "source_sha256": {path.name: oci_bundle.sha256(path) for path in
                  [Path(__file__), producer, Path(oci_bundle.__file__), Path(import_images.__file__),
                   Path(overlay.__file__), Path(overlay.__file__).with_name("extract_store.sh")]}, "stages": {}}

    def produce(label, selected_parts, base=None):
        output, receipt = root / (label + ".tar.gz"), root / (label + ".json")
        command = ["unshare", "--mount", "--pid", "--fork", "--kill-child", "--mount-proc", "--net",
                   sys.executable, str(producer), "--execution-environment", str(specification),
                   "--output", str(output), "--receipt", str(receipt), "--jobs", "2"]
        for part in selected_parts:
            command += ["--part", str(part)]
        if base:
            command += ["--base", str(base[0]), "--base-receipt", str(base[1])]
        lab.run(command)
        return output, receipt

    def snapshot_count():
        return max(0, len(lab.ctr.command("snapshots", "--snapshotter", "overlayfs", "list").splitlines()) - 1)

    def check_runtime(label, receipt, initial_unpacked=True):
        tags = sorted(receipt["tags"])
        lab.ctr.inventory(receipt["tags"], unpacked=initial_unpacked)
        before = snapshot_count()
        if not initial_unpacked:
            assert before == 0, "the shipped store must not contain unpacked snapshots"
        run_seconds = []
        selectors = {tag: tag for tag in tags}
        selectors.update({digest: tag for tag, digest in receipt["tags"].items()})
        for selector, tag in selectors.items():
            image = json.loads(lab.docker("image", "inspect", selector))[0]
            assert image["Id"] == receipt["tags"][tag]
            assert image["Architecture"] == architecture
            assert image["Os"] == "linux"
            assert image["RootFS"]["Layers"] == expected_by_tag[tag]["rootfs"]["diff_ids"]
            for key, value in expected_by_tag[tag]["config"].items():
                assert image["Config"].get(key) == value, (selector, key)
            started = time.monotonic()
            assert lab.docker("run", "--rm", "--network=none", "--pull=never", selector) == "SONIC_CONTAINERD_OK\n"
            run_seconds.append(time.monotonic() - started)
        lab.ctr.inventory(receipt["tags"], unpacked=True)
        after = snapshot_count()
        assert after > 0
        report["stages"][label] = {"tags": len(tags), "passed": True,
            "manifest_ids": len(set(receipt["tags"].values())), "config_and_diffids_preserved": True,
            "snapshots_before": before, "snapshots_after": after, "run_seconds": run_seconds,
            "first_use_unpacks_on_target": not initial_unpacked,
            "ownership_groups_hardlinks_whiteouts_opaque_directories": True}

    try:
        base = produce("base-store", parts[:1])
        base_before = [(oci_bundle.sha256(path), path.stat().st_mode, path.stat().st_uid) for path in base]
        final = produce("dockerfs", parts[1:], base)
        assert base_before == [(oci_bundle.sha256(path), path.stat().st_mode, path.stat().st_uid) for path in base]
        base_receipt, final_receipt = (json.loads(item[1].read_text()) for item in (base, final))
        assert base_receipt["imported_images"] == final_receipt["imported_images"] == 1
        assert base_receipt["unpacked"] is final_receipt["unpacked"] is False
        assert len(final_receipt["images"]) == 2 and len(final_receipt["tags"]) == 5
        assert final_receipt["base_sha256"] == base_receipt["sha256"]
        report["stages"]["cached_base"] = {"base_images": 1, "new_imports": 1, "final_images": 2,
            "base_unchanged": True, "changed_service_adds_new_layer": True,
            "producer_phases_seconds": final_receipt["phases_seconds"]}
        report["store_sha256"] = final_receipt["sha256"]
        filesystem, squashfs = host_fixture(root, lab)
        restored = root / "installed-dockerfs"
        restored.mkdir()
        extractor = Path(__file__).parent / "containerd_runtime/extract_store.sh"
        lab.run(["sh", "-c", 'exec sh "$1" "$2" "$3" < "$4"', "installer-extract",
                 str(extractor), str(squashfs), str(restored), str(final[0])])
        lab.containerd("installed", data_root=restored / "containerd-store")
        lab.start_docker("installed")
        check_runtime("installer_restored_different_root", final_receipt, initial_unpacked=False)
        # Docker load replaces an existing name while other version tags keep
        # the old image available for rollback. Exercise this with a new layer.
        updated_tag = "docker.io/library/docker-oci-probe:latest"
        old_digest = final_receipt["tags"][updated_tag]
        new_digest = final_receipt["tags"]["docker.io/library/docker-archive-probe:latest"]
        replacement = root / "replacement.tar"
        lab.docker("tag", new_digest, updated_tag)
        lab.docker("save", "--output", str(replacement), updated_tag)
        lab.docker("tag", old_digest, updated_tag)
        lab.docker("load", "--input", str(replacement))
        assert json.loads(lab.docker("image", "inspect", updated_tag))[0]["Id"] == new_digest
        assert lab.docker("run", "--rm", "--network=none", "--pull=never", updated_tag) == "SONIC_CONTAINERD_OK\n"
        lab.docker("tag", old_digest, updated_tag)
        check_runtime("docker_load_replacement_and_rollback", final_receipt)
        lab.stop()
        lab.containerd("installed", data_root=restored / "containerd-store")
        lab.start_docker("installed")
        check_runtime("daemon_restart", final_receipt)
        saved = root / "docker-save.tar"
        lab.docker("save", "--output", str(saved), *sorted(final_receipt["tags"]))
        lab.stop()
        lab.containerd("reload")
        lab.start_docker("reload")
        lab.docker("load", "--input", str(saved))
        check_runtime("docker_save_reload", final_receipt)
        lab.stop()
        command = overlay.INITRAMFS_EXTRACTOR + '\nrootmnt=$1\nsonic_bazel_extract_containerd --file=- < "$2"\n'
        lab.run(["sh", "-c", command, "initramfs-extract", str(filesystem), str(final[0])])
        lab.containerd("initramfs", data_root=filesystem / "var/lib/docker/containerd-store")
        lab.start_docker("initramfs")
        check_runtime("initramfs_restored_different_root", final_receipt, initial_unpacked=False)
        report["passed"] = True
    finally:
        lab.stop()
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
