#!/usr/bin/env python3
"""Diagnostic only. Run from the checkout with the same arguments as build_boot_bundle.py."""
import argparse
import base64
import gzip
from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.cache_reuse import json_records
from tools.bazel.oci.oci_layout import validate_layout
from tools.bazel.oci.boot_layout import direct_layout_for_archive

FILES = {"output": "bundle.tar.gz", "inventory": "inventory.json", "labels": "labels.json"}


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(source, dest):
    dest.parent.mkdir(exist_ok=True, parents=True)
    try:
        source.replace(dest)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as stream:
        partial = Path(stream.name)
    try:
        shutil.copy2(source, partial)
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)


DHCP = "docker-dhcp-relay.gz"
CHUNK = 1024 * 1024


def bazel_metrics(events):
    metrics = {}
    for line in events.open():
        event = json.loads(line)
        command = event.get("structuredCommandLine", {})
        if command.get("commandLineLabel") == "original":
            for section in command.get("sections", []):
                for option in section.get("optionList", {}).get("option", []):
                    key = option.get("optionName")
                    if key in ("startup_time", "command_wait_time", "extract_data_time"):
                        metrics[key + "_ms"] = int(option["optionValue"])
        for row in event.get("buildToolLogs", {}).get("log", []):
            if row.get("name") not in ("elapsed time", "critical path") or "contents" not in row:
                continue
            value = base64.b64decode(row["contents"]).decode()
            if row["name"] == "elapsed time":
                metrics["bazel_elapsed_seconds"] = float(value)
            else:
                match = re.search(r"Critical Path:\s*([\d.]+)s", value)
                if match:
                    metrics["critical_path_seconds"] = float(match.group(1))
        if "buildMetrics" in event:
            metrics["timing_metrics_ms"] = {key: int(value) for key, value in
                event["buildMetrics"].get("timingMetrics", {}).items() if key.endswith("InMs")}
    return metrics


def drive_bazel(command):
    # Only allow disk cache writes under this benchmark's private directory.
    opts = ["--remote_cache=", "--remote_executor=", "--experimental_remote_downloader=",
            "--remote_accept_cached=true", "--remote_upload_local_results=true",
            "--disk_cache=" + os.environ["PAIRED_DISK_CACHE"], "--noannounce_rc", "--symlink_prefix=/"]
    startup = ["--output_base=" + os.environ["PAIRED_OUTPUT_BASE"]]
    start = time.monotonic()
    proc = subprocess.run([os.environ["PAIRED_BAZEL"], *startup, *command, *(opts if command[0] == "build" else [])])
    elapsed = time.monotonic() - start
    if proc.returncode or command[0] != "build":
        return proc.returncode
    events = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--build_event_json_file=")))
    spawns = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--execution_log_json_file=")))
    metrics = bazel_metrics(events)
    metrics["bazel_subprocess_wall_seconds"] = elapsed
    targets = []
    for spawn in json_records(spawns):
        mnemonic = spawn.get("mnemonic", "")
        if mnemonic.startswith("SonicBoot"):
            record = {"action": mnemonic, "target": spawn.get("targetLabel", "").split("//")[-1],
                      "result": "hit" if spawn.get("cacheHit") else "executed"}
            raw = spawn.get("metrics", {})
            record["durations"] = {key: val for key, val in raw.items() if key.endswith("Time")
                                   and isinstance(val, (int, str)) and re.fullmatch(r"[\d.]+s?", str(val))}
            targets.append(record)
    evidence = Path(os.environ["PAIRED_EVIDENCE"])
    write_json(evidence / (events.stem + "-bazel.json"), metrics)
    write_json(evidence / (spawns.stem + "-targets.json"), targets)
    return 0


def stage_control(selections, stage, platform, token):
    """Only the relay archive gets a new path/inode. All other images keep their direct handoff."""
    start = time.monotonic()
    inputs, rows, expected = [], [], {}
    stage.mkdir()
    for option, source, tags in selections:
        source = source.absolute()
        layout = direct_layout_for_archive(source, token)
        if layout is None:
            raise ValueError(f"No matching direct handoff for selected {source.name}")
        image = validate_layout(layout, platform, verify_layer_digest=False)
        digest = image.manifest["config"]["digest"]
        info = {"config": digest, "diff_ids": image.config["rootfs"]["diff_ids"],
                "labels": image.config["config"].get("Labels") or {}}
        for tag in tags.split(","):
            if expected.setdefault(tag, info) != info:
                raise ValueError("Conflicting selected tag " + tag)
        dest = source
        route = "producer layout"
        if source.name == DHCP:
            dest = stage / source.name
            subprocess.run(["cp", "--dereference", "--reflink=auto", "--sparse=auto", "--", source, dest], check=True)
            if source.samefile(dest):
                raise ValueError("Control relay archive must have an independent inode")
            route = "archive check"
        rows.append({"name": source.name, "bytes": source.stat().st_size, "candidate": "producer layout", "control": route})
        inputs.append((option, dest, tags))
    if sum(row["name"] == DHCP for row in rows) != 1:
        raise ValueError("Require exactly one selected DHCP relay runtime archive")
    return inputs, rows, expected, time.monotonic() - start


def run_arm(arm, opts, selections, state, work, result, token, version):
    evidence = result / arm
    evidence.mkdir()
    arm_work = work / arm
    arm_work.mkdir()
    output = {key: arm_work / name for key, name in FILES.items()}
    seed = state / arm / "seed"
    seed.parent.mkdir(exist_ok=True)
    if version == "a":
        seed.mkdir()
    elif not (seed / "ac").is_dir():
        raise ValueError("Missing version-A action cache for " + arm)
    launcher = arm_work / "bazel"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(Path(__file__).resolve()), "--drive-bazel"]) + ' "$@"\n')
    launcher.chmod(0o755)
    real = shutil.which(os.environ.get("BAZEL", "bazel"))
    if not real:
        raise RuntimeError("Could not find the build's Bazel executable")
    with tempfile.TemporaryDirectory(dir=arm_work) as session:
        session = Path(session)
        disk = seed
        if version != "a":
            # Bazel writes CAS/AC by atomic renames. A private snapshot ensures
            # every timed B arm starts from its own A seed, including repeat B.
            disk = session / "cache"
            subprocess.run(["cp", "-al", "--", seed, disk], check=True)
        env = dict(os.environ, BAZEL=str(launcher), PAIRED_BAZEL=real, PAIRED_OUTPUT_BASE=str(session/"base"),
                   PAIRED_DISK_CACHE=str(disk), PAIRED_EVIDENCE=str(evidence), SONIC_VS_BOOT_HANDOFF=token,
                   SONIC_BOOT_BUNDLE_EVIDENCE_DIR=str(evidence))
        cmd = [sys.executable, str(ROOT / "tools/bazel/image/build_boot_bundle.py"), "--platform", opts.platform]
        for arg, path in output.items():
            cmd.extend(["--" + arg, str(path)])
        for option, source, tags in selections:
            cmd.extend((option, str(source), tags))
        try:
            start = time.monotonic()
            code = subprocess.run(cmd, env=env).returncode
            seconds = time.monotonic() - start
            if code:
                raise RuntimeError(f"{arm} packager exited {code}")
        finally:
            subprocess.run([str(launcher), "shutdown"], env=env, stdout=subprocess.DEVNULL, check=False)
    receipt, = evidence.glob("boot-*.json")
    phases = json.loads(receipt.read_bytes())
    fallback = int(arm == "control")
    if phases["bundle"]["inputs"] != dict(direct_oci=len(selections)-fallback, verified_sidecar=0, archive_only=fallback):
        raise ValueError(f"{arm} routing differs from the requested comparison")
    if ("archives" in phases) != bool(fallback):
        raise ValueError(f"Unexpected archive phase for {arm}")
    if fallback and sum(phases["archives"]["actions"]["SonicBootArchive"].values()) != 1:
        raise ValueError("Expected exactly one DHCP archive action")
    return output, {"consumer_wall_seconds": seconds, "phases": phases}


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


def verify_archive(path, expected):
    from tools.bazel.image.boot_image_bundle import blob_name, qualified_tag

    payloads, observed, docker, layer_diff = {}, {}, {}, {}
    start = time.monotonic()
    with tarfile.open(path, mode="r|gz", bufsize=CHUNK) as tar:
        for member in tar:
            name = member.name
            if not member.isfile() or name in observed:
                raise ValueError("Unexpected or duplicate bundle member")
            reader = HashReader(tar.extractfile(member))
            if name in layer_diff:
                raw = hashlib.sha256()
                with gzip.GzipFile(fileobj=reader) as expanded:
                    while chunk := expanded.read(CHUNK):
                        raw.update(chunk)
                if "sha256:" + raw.hexdigest() != layer_diff[name]:
                    raise ValueError(f"Uncompressed layer differs from config: {name}")
            else:
                if member.size > 16 * CHUNK:
                    raise ValueError("Unreferenced or oversized metadata blob")
                payloads[name] = json.loads(reader.read())
                if name == "manifest.json":
                    for image in payloads[name]:
                        for tag in image["RepoTags"]:
                            if tag in docker or tag not in expected:
                                raise ValueError(f"Unexpected Docker tag: {tag}")
                            docker[tag] = image
                            want = expected[tag]
                            if image["Config"] != blob_name(want["config"]):
                                raise ValueError(f"Wrong config for {tag}")
                            for blob, diff in zip(image["Layers"], want["diff_ids"], strict=True):
                                if layer_diff.setdefault(blob, diff) != diff:
                                    raise ValueError("Conflicting DiffIDs for a selected blob")
            if name.startswith("blobs/") and name != blob_name("sha256:" + reader.hash.hexdigest()):
                raise ValueError(f"Content address mismatch: {name}")
            observed[name] = member.size
    if set(docker) != set(expected) or not set(layer_diff).issubset(observed):
        raise ValueError("Missing Docker tag or layer")
    if payloads["oci-layout"] != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("Wrong OCI layout")
    def referenced(desc):
        name = blob_name(desc["digest"])
        if observed[name] != desc["size"]:
            raise ValueError("Wrong descriptor size: " + name)
        return name
    aliases = {qualified_tag(tag): tag for tag in expected}
    seen = set()
    for desc in payloads["index.json"]["manifests"]:
        ref = desc["annotations"]["org.opencontainers.image.ref.name"]
        tag = aliases[ref]
        if tag in seen or ref != desc["annotations"]["io.containerd.image.name"]:
            raise ValueError("Unexpected OCI tag: " + tag)
        seen.add(tag)
        image = payloads[referenced(desc)]
        config = referenced(image["config"])
        if config != docker[tag]["Config"] or payloads[config]["rootfs"]["diff_ids"] != expected[tag]["diff_ids"]:
            raise ValueError("OCI/Docker config mismatch: " + tag)
        if [referenced(layer) for layer in image["layers"]] != docker[tag]["Layers"]:
            raise ValueError("OCI/Docker layer order mismatch: " + tag)
    if seen != set(expected):
        raise ValueError("OCI tag set differs from Make")
    metrics = dict(images=len(payloads["manifest.json"]), tags=len(docker), encoded_layers=len(layer_diff),
                   unique_diff_ids=len(set(layer_diff.values())), checked_members=len(observed),
                   verification_seconds=time.monotonic()-start)
    return metrics, payloads["manifest.json"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--platform", required=True)
    for key in FILES:
        p.add_argument("--" + key, type=Path, required=True)
    for key in ("image", "package-image"):
        p.add_argument("--" + key, action="append", nargs=2, default=[])
    opts = p.parse_args()
    selected = [("--" + key, Path(name), tags) for key in ("image", "package-image")
                for name, tags in getattr(opts, key.replace("-", "_"))]
    releases = {tag.rsplit(":", 1)[-1] for _, _, tags in selected for tag in tags.split(",")
                if re.match(r"^oci-bench-[ab]-", tag.rsplit(":", 1)[-1])}
    if len(releases) != 1:
        raise ValueError("Expected a single oci-bench-a-* or oci-bench-b-* release")
    release, = releases
    side = release[len("oci-bench-")]
    token = os.environ.get("SONIC_VS_BOOT_HANDOFF", "")
    if not token:
        raise ValueError("The Make installer must provide its current producer handoff")
    state = ROOT / "target/.direct-bench"
    state.mkdir(parents=True, exist_ok=True)
    artifact = ROOT / "artifacts/vs/direct-bench" / ("version-" + side)
    artifact.mkdir(parents=True, exist_ok=True)
    item = Path(tempfile.mkdtemp(dir=artifact, prefix=f"selection-{len(selected)}-"))
    with tempfile.TemporaryDirectory(dir=state, prefix="stage-") as tmp:
        work = Path(tmp)
        baseline, rows, expected, setup = stage_control(selected, work / "control-inputs", opts.platform, token)
        write_json(item / "sources.json", rows)
        write_json(item / "expected.json", expected)
        orders = [["control", "candidate"]] if side == "a" else [["candidate", "control"], ["control", "candidate"]]
        reports = []
        all_files = []
        for trial, arms in enumerate(orders, start=1):
            pair = work / f"pair-{trial}"
            evidence = item / f"pair-{trial}"
            pair.mkdir()
            evidence.mkdir()
            report = {"version":release, "order":arms, "control_preparation_seconds":setup, "arms":{}}
            files = {}
            for arm in arms:
                files[arm], stats = run_arm(arm, opts, selected if arm=="candidate" else baseline,
                                            state, pair, evidence, token, side)
                report["arms"][arm] = stats
                write_json(evidence / "report.json", report)
            report["consumer_seconds_saved"] = (report["arms"]["control"]["consumer_wall_seconds"]
                                                -report["arms"]["candidate"]["consumer_wall_seconds"])
            write_json(evidence / "report.json", report)
            reports.append((evidence, report))
            all_files.append(files)
        # Deep verification happens after *all* timed pairs on this release.
        verified = {}
        for (evidence, report), files in zip(reports, all_files, strict=True):
            hashes = {key: {arm: {"sha256": sha256(outputs[key]), "bytes":outputs[key].stat().st_size}
                            for arm,outputs in files.items()} for key in FILES}
            report["outputs"] = hashes
            report["byte_identical"] = all(pair["control"] == pair["candidate"] for pair in hashes.values())
            for arm, outputs in files.items():
                if json.loads(outputs["inventory"].read_bytes()) != {tag: spec["config"] for tag,spec in expected.items()}:
                    raise ValueError("Emitted inventory differs from producer tag/config map")
                if json.loads(outputs["labels"].read_bytes()) != {tag: spec["labels"] for tag,spec in expected.items()}:
                    raise ValueError("Emitted labels differ from producer config")
                checksum = hashes["output"][arm]["sha256"]
                if checksum not in verified:
                    verified[checksum] = verify_archive(outputs["output"], expected)
                summary, manifest = verified[checksum]
                write_json(evidence / arm / "verification.json", summary)
                write_json(evidence / arm / "docker-manifest.json", manifest)
            if any(hashes[k]["control"] != hashes[k]["candidate"] for k in ("labels","inventory")):
                raise ValueError("Inventory/labels are not byte-identical")
            report["runtime_content_identical"] = True
            write_json(evidence / "report.json", report)
        final_files = all_files[-1]["candidate"]
        for key in ("inventory", "labels"):
            shutil.copyfile(final_files[key], item / FILES[key])
        for key in ("inventory", "labels", "output"):
            publish(final_files[key], getattr(opts, key))
        print("Paired steps complete:", item.relative_to(ROOT), [round(r["consumer_seconds_saved"],2) for _,r in reports],
              flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--drive-bazel"]:
        raise SystemExit(drive_bazel(sys.argv[2:]))
    main()
