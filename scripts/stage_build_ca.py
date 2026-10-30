#!/usr/bin/env python3
"""Stage a public CA bundle as native-builder trust anchors and print its digest."""

import argparse
import hashlib
from pathlib import Path
import re
import ssl
import subprocess
import tempfile


CERTIFICATE = re.compile(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", re.S)
ANCHOR_NAME = re.compile(r"[0-9a-f]{64}\.crt")


def certificates(contents):
    matches = list(CERTIFICATE.finditer(contents))
    if not matches or CERTIFICATE.sub(b"", contents).strip():
        raise ValueError("CA bundle must contain only public PEM certificates")
    anchors = {}
    for match in matches:
        result = subprocess.run(["openssl", "x509", "-inform", "PEM", "-outform", "DER"],
                                input=match.group() + b"\n", capture_output=True)
        if result.returncode or not result.stdout:
            raise ValueError("CA bundle contains an invalid X.509 certificate")
        name = hashlib.sha256(result.stdout).hexdigest() + ".crt"
        anchors[name] = ssl.DER_cert_to_PEM_cert(result.stdout).encode("ascii")
    return anchors


def stage(bundle, output):
    contents = bundle.read_bytes()
    anchors = certificates(contents)
    output = output.absolute()
    if output.resolve() != output or output.is_symlink():
        raise ValueError("CA staging directory must not be linked")
    output.mkdir(parents=True, exist_ok=True)
    existing = list(output.iterdir())
    if any(not path.is_file() or path.is_symlink() or not ANCHOR_NAME.fullmatch(path.name)
           for path in existing):
        raise ValueError("CA staging directory contains unrelated or linked files")
    for name, certificate in sorted(anchors.items()):
        destination = output / name
        if destination.exists() and destination.read_bytes() == certificate:
            continue
        with tempfile.NamedTemporaryFile(dir=output, prefix=".ca-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(certificate)
        try:
            temporary.chmod(0o644)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    for path in existing:
        if path.name not in anchors:
            path.unlink()
    return hashlib.sha256(contents).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        digest = stage(args.bundle, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, "Cannot stage build CA certificates: " + str(error) + "\n")
    print(digest)


if __name__ == "__main__":
    main()
