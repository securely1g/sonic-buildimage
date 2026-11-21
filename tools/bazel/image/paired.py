#!/usr/bin/env python3
"""Diagnostic only. Run from the checkout with the same arguments as build_boot_bundle.py."""
import argparse
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
from tools.bazel.image.build_boot_bundle import complete_layout
from tools.bazel.oci.boot_layout import direct_layout_for_archive

FILES = {"output": "bundle.tar.gz", "inventory": "inventory.json", "labels": "labels.json"}


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def drive_bazel(command):
    # Explicitly override RC- and environment-supplied caches at the last CLI
    # position. Keep the build's repository cache, downloader and toolchain args.
    opts = ["--remote_cache=", "--remote_executor=", "--remote_accept_cached=true",
            "--remote_upload_local_results=true", "--disk_cache=" + os.environ["PAIRED_DISK_CACHE"],
            "--noannounce_rc", "--symlink_prefix=/"]
    binary = os.environ["PAIRED_BAZEL"]
    startup = ["--output_base=" + os.environ["PAIRED_OUTPUT_BASE"]]
    extra = opts if command[0] == "build" else []
    proc = subprocess.run([binary, *startup, *command, *extra])
    if proc.returncode:
        return proc.returncode
    for arg in command:
        if arg.startswith("--execution_log_json_file="):
            path = Path(arg.split("=", 1)[1])
            evidence = Path(os.environ["PAIRED_EVIDENCE"])
            # Only extract public target labels and counts. The raw log may
            # contain private downloader options and is not a benchmark artifact.
            rows = Counter()
            for action in json_records(path):
                mnemonic = action.get("mnemonic", "")
                if mnemonic.startswith("SonicBoot"):
                    target = action.get("targetLabel", "").split("//")[-1]
                    rows[mnemonic, target, "hit" if action.get("cacheHit") else "executed"] += 1
            write_json(evidence / (path.stem + "-targets.json"),
                       [dict(action=m, target=t, result=k, count=n) for (m, t, k), n in sorted(rows.items())])
    return 0


def stage_control(selections, stage, platform, token):
    start = time.monotonic()
    inputs, paths, rows = [], {}, []
    for option, source, tags in selections:
        source = source.absolute()  # Receipts follow the selected symlink's logical name.
        if source in paths:
            dest = paths[source]
        elif source.is_dir():
            dest = source
            paths[source] = dest
            rows.append({"name": source.name, "candidate": "explicit layout", "control": "explicit layout"})
        else:
            dest = stage / str(len(paths)) / source.name
            dest.parent.mkdir(parents=True)
            subprocess.run(["cp", "--dereference", "--reflink=auto", "--sparse=auto", "--", source, dest], check=True)
            if source.samefile(dest):
                raise ValueError("control must have its own archive inode")
            paths[source] = dest
            store = source.parent / ".boot-image-layouts"
            receipt = store / (source.name + ".json")
            control_store = dest.parent / ".boot-image-layouts"
            control_store.mkdir()
            control_receipt = control_store / (dest.name + ".json")
            generation = direct_layout_for_archive(source, token)
            if generation is not None and complete_layout(generation, platform):
                # Stock produces this SHA stamp upstream. Computing it in setup
                # gives the control the same verified-sidecar path as master.
                write_json(control_receipt, {"archive": sha256(dest), "index": generation.name})
                (control_store / generation.name).symlink_to(generation, target_is_directory=True)
                routing = {"candidate": "producer layout", "control": "verified sidecar"}
            else:
                routing = {"candidate": "archive check", "control": "archive check"}
                if receipt.is_file():
                    shutil.copyfile(receipt, control_receipt)
                    try:
                        index = json.loads(control_receipt.read_bytes())["index"]
                        if re.fullmatch(r"[0-9a-f]{64}", index) and (store / index).is_dir():
                            (control_store / index).symlink_to(store / index, target_is_directory=True)
                    except (KeyError, TypeError, ValueError):
                        pass
            rows.append({"name": source.name, "bytes": source.stat().st_size, **routing})
        inputs.append((option, dest, tags))
    # Resolve the generated Bazel archive labels back to logical Make filenames
    # even when optional packages supply a second input with the same basename.
    for arm in ("control", "candidate"):
        occurrences = Counter()
        for row in rows:
            if row[arm] in ("explicit layout", "producer layout"):
                continue
            key = hashlib.sha256(row["name"].encode()).hexdigest()
            slot = occurrences[key]
            row[arm + "_archive_target"] = "archives:source_" + key + (f"_{slot}" if slot else "")
            occurrences[key] += 1
    return inputs, rows, time.monotonic() - start


def run_arm(arm, opts, selections, state, work, result, token):
    evidence = result / arm
    evidence.mkdir()
    arm_work = work / arm
    arm_work.mkdir()
    output = {key: arm_work / name for key, name in FILES.items()}
    base = arm_work / "output-base"
    disk = state / arm / "disk-cache"
    disk.mkdir(parents=True, exist_ok=True)
    launcher = arm_work / "bazel"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(Path(__file__).resolve()), "--drive-bazel"]) + ' "$@"\n')
    launcher.chmod(0o755)
    real = shutil.which(os.environ.get("BAZEL", "bazel"))
    if not real:
        raise RuntimeError("could not find the build's Bazel executable")
    env = dict(os.environ, BAZEL=str(launcher), PAIRED_BAZEL=real, PAIRED_OUTPUT_BASE=str(base),
               PAIRED_DISK_CACHE=str(disk), PAIRED_EVIDENCE=str(evidence), SONIC_VS_BOOT_HANDOFF=token,
               SONIC_BOOT_BUNDLE_EVIDENCE_DIR=str(evidence))
    cmd = [sys.executable, str(ROOT / "tools/bazel/image/build_boot_bundle.py"), "--platform", opts.platform]
    for arg, path in output.items():
        cmd.extend(["--" + arg, str(path)])
    for option, source, tags in selections:
        cmd += [option, str(source), tags]
    try:
        start = time.monotonic()
        code = subprocess.run(cmd, env=env).returncode
        seconds = time.monotonic() - start
        if code:
            raise RuntimeError(f"{arm} packager exited {code}")
    finally:
        # Include every build/startup in seconds, exclude diagnostic server removal.
        subprocess.run([str(launcher), "shutdown"], env=env, stdout=subprocess.DEVNULL, check=False)
    receipt, = evidence.glob("boot-*.json")
    runs = json.loads(receipt.read_bytes())
    return output, {"consumer_wall_seconds": seconds, "phases": runs}


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
    release = os.environ.get("SONIC_IMAGE_VERSION", "")
    if not re.match(r"^oci-bench-[ab]", release):
        candidates = {tag.rsplit(":", 1)[-1] for _, _, tags in selected for tag in tags.split(",")
                      if re.match(r"^oci-bench-[ab]", tag.rsplit(":", 1)[-1])}
        if len(candidates) != 1:
            raise ValueError("expected an oci-bench-a or oci-bench-b SONiC version")
        release, = candidates
    side = release[len("oci-bench-")]
    token = os.environ.get("SONIC_VS_BOOT_HANDOFF", "")
    if not token:
        raise ValueError("the VS installer did not receive its Make handoff")
    state = ROOT / "target/.direct-bench"
    state.mkdir(parents=True, exist_ok=True)
    artifact = ROOT / "artifacts/vs/direct-bench" / ("version-" + side)
    artifact.mkdir(parents=True, exist_ok=True)
    # Extras or retries may invoke the packager again with additional inputs.
    item = Path(tempfile.mkdtemp(dir=artifact, prefix=f"selection-{len(selected)}-"))
    with tempfile.TemporaryDirectory(dir=state, prefix="stage-") as tmp:
        work = Path(tmp)
        baseline, rows, prepare_seconds = stage_control(selected, work / "control-inputs", opts.platform, token)
        write_json(item / "sources.json", rows)
        if not any(row["candidate"] == "producer layout" for row in rows):
            raise ValueError("no current native producer layout in installer input list")
        arms = ["control", "candidate"] if side == "a" else ["candidate", "control"]
        report = {"version": release, "order": arms, "control_preparation_seconds": prepare_seconds, "arms": {}}
        files = {}
        for arm in arms:
            active = arm == "candidate"
            files[arm], stats = run_arm(arm, opts, selected if active else baseline, state, work, item,
                                        token if active else "")
            report["arms"][arm] = stats
            write_json(item / "report.json", report)
        hashes = {}
        for key in FILES:
            hashes[key] = {arm: {"sha256": sha256(files[arm][key]), "bytes": files[arm][key].stat().st_size}
                           for arm in arms}
        report["outputs"] = hashes
        report["byte_identical"] = all(pair["control"] == pair["candidate"] for pair in hashes.values())
        report["consumer_seconds_saved"] = (report["arms"]["control"]["consumer_wall_seconds"]
                                          - report["arms"]["candidate"]["consumer_wall_seconds"])
        write_json(item / "report.json", report)
        if not report["byte_identical"]:
            raise ValueError("direct OCI handoff changed installer output")
        # The Docker manifest is the first member. Retain the actual selected
        # config IDs, tags and ordered layer digests without re-reading all blobs.
        with tarfile.open(files["candidate"]["output"], mode="r|gz") as tar:
            member = tar.next()
            if not member or member.name != "manifest.json":
                raise ValueError("missing leading Docker manifest")
            write_json(item / "docker-manifest.json", json.load(tar.extractfile(member)))
        for key in ("inventory", "labels"):
            shutil.copyfile(files["candidate"][key], item / FILES[key])
        for key in ("inventory", "labels", "output"):
            publish(files["candidate"][key], getattr(opts, key))
        print(f"direct OCI paired benchmark {item.relative_to(ROOT)}: "
              f"baseline {report['arms']['control']['consumer_wall_seconds']:.1f}s, "
              f"candidate {report['arms']['candidate']['consumer_wall_seconds']:.1f}s", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--drive-bazel"]:
        raise SystemExit(drive_bazel(sys.argv[2:]))
    main()
