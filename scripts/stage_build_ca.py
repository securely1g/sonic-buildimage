#!/usr/bin/env python3
"""Stage a CA-only bundle for native builders, or remove its managed staging."""

import argparse
import base64
import hashlib
from pathlib import Path
import re
import ssl
import tempfile


MAX_BUNDLE_BYTES = 8 * 1024 * 1024
CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----"
)
ANCHOR_NAME = re.compile(r"[0-9a-f]{64}\.crt")


def certificates(contents):
    """Validate the entire input before returning normalized, unique CA anchors."""
    if not isinstance(contents, bytes) or len(contents) > MAX_BUNDLE_BYTES:
        raise ValueError("CA bundle must be at most 8 MiB of PEM certificates")
    anchors, offset = {}, 0
    for match in CERTIFICATE.finditer(contents):
        if contents[offset:match.start()].strip():
            raise ValueError("CA bundle must contain certificates only")
        try:
            der = base64.b64decode(re.sub(rb"\s", b"", match.group(1)), validate=True)
            pem = ssl.DER_cert_to_PEM_cert(der)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=pem)
            if context.get_ca_certs(binary_form=True) != [der]:
                raise ValueError("certificate must contain exactly one CA trust anchor")
        except (ValueError, ssl.SSLError) as error:
            raise ValueError("CA bundle contains an invalid CA certificate") from error
        anchors[hashlib.sha256(der).hexdigest() + ".crt"] = pem.encode("ascii")
        offset = match.end()
    if not anchors or contents[offset:].strip():
        raise ValueError("CA bundle must contain certificates only")
    return anchors


def managed_directory(output):
    """Refuse linked paths and unrelated entries before changing staged files."""
    output = Path(output).absolute()
    if output.resolve() != output or output.is_symlink():
        raise ValueError("CA staging directory must not be linked")
    existing = list(output.iterdir()) if output.exists() else []
    if any(not path.is_file() or path.is_symlink() or not ANCHOR_NAME.fullmatch(path.name)
           for path in existing):
        raise ValueError("CA staging directory contains unrelated or linked files")
    return output, existing


def stage(bundle, output):
    """Freeze one bounded read; publish anchors and digest from those same bytes."""
    bundle = Path(bundle)
    if not bundle.is_file():
        raise ValueError("CA bundle must be a regular file")
    with bundle.open("rb") as stream:
        contents = stream.read(MAX_BUNDLE_BYTES + 1)
    anchors = certificates(contents)
    output, existing = managed_directory(output)
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o755)
    for name, certificate in sorted(anchors.items()):
        destination = output / name
        if destination.exists() and destination.read_bytes() == certificate:
            destination.chmod(0o644)
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


def clear(output):
    """Remove only the validated staging directory and its managed CA files."""
    output, existing = managed_directory(output)
    for path in existing:
        path.unlink()
    if output.exists():
        output.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="CA-only PEM file; omit to clear staging")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.bundle is None:
            clear(args.output)
        else:
            print(stage(args.bundle, args.output))
    except (OSError, ValueError) as error:
        parser.exit(1, "Cannot stage build CA certificates: " + str(error) + "\n")


if __name__ == "__main__":
    main()
