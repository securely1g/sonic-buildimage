"""Validate services through the existing VS installer's serial login session."""

import base64
import hashlib
import json
from pathlib import Path
import shlex
import time
import zlib

from tools.ci.guest_probe import MARKER, SERVICES


def probe_command():
    source = Path(__file__).with_name("guest_probe.py").read_bytes()
    encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
    program = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(encoded) + ")))"
    command = "sudo -n python3 -c " + shlex.quote(program)
    # A serial TTY's canonical input buffer is commonly 4096 bytes. Keep a
    # bounded compressed payload rather than silently truncating a script.
    if len(command.encode()) >= 3500:
        raise ValueError("Guest probe exceeds the serial command limit")
    return command


def passed(value):
    if value.get("schema_version") != 1 or value.get("status") != "passed":
        return False
    if set(value.get("services", {})) != set(SERVICES):
        return False
    for service in value["services"].values():
        processes = service.get("critical_processes", {})
        if service.get("errors") or not processes or any(state != "RUNNING" for state in processes.values()):
            return False
    return value.get("database_ping") == {"returncode": 0, "output": "PONG"}


def validate(session, prompt, report, image, timeout=600, clock=time.monotonic, sleep=time.sleep):
    if timeout <= 0:
        raise ValueError("A positive guest validation timeout is required")
    report, image = Path(report), Path(image)
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.", "generator": "tools/ci/guest_validation.py"},
        "status": "failed", "scope": "single-ASIC VS boot and core services; forwarding is not tested",
        "image": str(image), "probes": [],
    }
    started = clock()
    try:
        digest = hashlib.sha256()
        with image.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        result["image_sha256"] = digest.hexdigest()
        payload = probe_command()
        # Require a real output line, not the terminal's echoed command. An
        # end-of-buffer anchor also matches partial serial reads, so wait for
        # the actual newline before parsing the JSON receipt.
        pattern = r"(?m)^" + MARKER + r"([^\r\n]+)\r?\n"
        while clock() - started < timeout:
            remaining = timeout - (clock() - started)
            session.sendline(payload)
            session.expect(pattern, timeout=max(1, min(180, remaining)))
            value = json.loads(session.match.group(1))
            result["probes"].append(value)
            session.expect(prompt, timeout=max(1, min(30, timeout - (clock() - started))))
            if passed(value):
                result["status"] = "passed"
                return result
            sleep(min(10, max(0, timeout - (clock() - started))))
        raise RuntimeError("VS guest core services did not become healthy before the deadline")
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(clock() - started, 3)
        report.write_text(json.dumps(result, indent=2) + "\n")


def verify_report(report, compressed_image):
    """Tie the successful guest probe to the actual uploaded disk image."""
    import gzip
    result = json.loads(Path(report).read_text())
    if result.get("status") != "passed" or not result.get("probes") or not passed(result["probes"][-1]):
        raise ValueError("Guest validation receipt does not record a successful probe")
    digest = hashlib.sha256()
    with gzip.open(compressed_image, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != result.get("image_sha256"):
        raise ValueError("Uploaded VS image does not match the validated guest disk")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Verify the guest receipt against a compressed VS disk")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    arguments = parser.parse_args()
    verify_report(arguments.report, arguments.image)
    print("VS guest service receipt matches the uploaded image")
