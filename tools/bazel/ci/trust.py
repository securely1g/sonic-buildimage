#!/usr/bin/env python3
"""Validate an explicit local CA input and install execution-only trust stores.

The certificate bytes stay in the local worker context. Public receipts contain
only their digest and count. An empty staged file means no additional trust.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import zipfile


TRUST_DIRECTORY = Path("usr/local/share/sonic-build-trust")
CERTIFICATE_DIRECTORY = Path("usr/local/share/ca-certificates/sonic-build-trust")
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----"
)


def parse_bundle(data, allow_empty=False):
    """Return unique normalized PEM certificates, rejecting other content."""
    if not isinstance(data, bytes) or len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("CA bundle must be at most 8 MiB of PEM certificates")
    if not data.strip():
        if allow_empty:
            return []
        raise ValueError("CA bundle is empty")
    certificates, seen, offset = [], set(), 0
    for match in CERTIFICATE.finditer(data):
        if data[offset:match.start()].strip():
            raise ValueError("CA bundle must contain certificates only")
        try:
            der = base64.b64decode(re.sub(rb"\s", b"", match.group(1)), validate=True)
            pem = ssl.DER_cert_to_PEM_cert(der).encode("ascii")
            # Decode with the TLS implementation, not just the PEM/base64 syntax.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=pem.decode("ascii"))
            if not context.get_ca_certs(binary_form=True):
                raise ValueError("certificate is not a CA trust anchor")
        except (ValueError, ssl.SSLError) as error:
            raise ValueError("CA bundle contains an invalid CA certificate") from error
        digest = hashlib.sha256(der).hexdigest()
        if digest not in seen:
            certificates.append(pem)
            seen.add(digest)
        offset = match.end()
    if not certificates or data[offset:].strip():
        raise ValueError("CA bundle must contain certificates only")
    return certificates


def _read(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("CA bundle must be a regular file")
    with path.open("rb") as stream:
        return stream.read(MAX_BUNDLE_BYTES + 1)


def _metadata(data, certificates):
    return {"enabled": bool(certificates), "sha256": hashlib.sha256(data).hexdigest(),
            "certificate_count": len(certificates)}


def validate_bundle(path, allow_empty=False):
    data = _read(path)
    return _metadata(data, parse_bundle(data, allow_empty=allow_empty))


def _write_certificates(certificates, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o755)
    paths = []
    for certificate in certificates:
        der = ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
        path = directory / (hashlib.sha256(der).hexdigest() + ".crt")
        with path.open("xb") as stream:
            stream.write(certificate)
        path.chmod(0o644)
        paths.append(path)
    return paths


def stage_bundle(source, destination, certificates_output=None):
    """Copy exact validated bytes, or create an empty opt-out file, exclusively."""
    data = b"" if source is None else _read(source)
    certificates = parse_bundle(data, allow_empty=source is None)
    with Path(destination).open("xb") as stream:
        stream.write(data)
    if certificates_output is not None:
        _write_certificates(certificates, certificates_output)
    return _metadata(data, certificates)


def _run(argv):
    result = subprocess.run([str(value) for value in argv], capture_output=True, text=True)
    if result.returncode:
        # Do not publish tool output that might include certificate contents.
        raise RuntimeError("execution trust-store command failed: " + Path(argv[0]).name)


def _extract_jdk(bazel, destination):
    """Extract only regular files from the already hash-verified Bazel binary."""
    prefix = PurePosixPath("embedded_tools/jdk")
    with zipfile.ZipFile(bazel) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if not path.is_relative_to(prefix):
                continue
            relative = path.relative_to(prefix)
            if ".." in relative.parts or "\\" in member.filename:
                raise ValueError("invalid path in Bazel embedded JDK")
            output = destination.joinpath(*relative.parts)
            mode = member.external_attr >> 16
            if member.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            if not stat.S_ISREG(mode):
                raise ValueError("Bazel embedded JDK contains a nonregular file")
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output.open("xb") as target:
                shutil.copyfileobj(source, target)
            output.chmod(mode & 0o777)
    for name in ("bin/keytool", "bin/java", "lib/security/cacerts"):
        if not (destination / name).is_file():
            raise ValueError("Bazel binary lacks its embedded Java trust tools")


def install_bundle(bundle, bazel, root=Path("/")):
    """Install an optional bundle into the disposable execution worker only."""
    data = _read(bundle)
    certificates = parse_bundle(data, allow_empty=True)
    metadata = _metadata(data, certificates)
    root = Path(root)
    trust = root / TRUST_DIRECTORY
    certs = root / CERTIFICATE_DIRECTORY
    trust.mkdir(parents=True, exist_ok=False)
    trust.chmod(0o755)
    if not certificates:
        receipt = trust / "receipt.json"
        receipt.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        receipt.chmod(0o644)
        return metadata
    paths = _write_certificates(certificates, certs)
    _run([root / "usr/sbin/update-ca-certificates"])
    # Client override variables select one file, so it must retain the
    # distribution's public roots as well as the explicitly supplied anchors.
    combined = trust / "ca-bundle.pem"
    shutil.copyfile(root / "etc/ssl/certs/ca-certificates.crt", combined)
    metadata["installed_bundle_sha256"] = hashlib.sha256(combined.read_bytes()).hexdigest()
    store = trust / "java-cacerts"
    with tempfile.TemporaryDirectory(prefix="sonic-build-trust-") as temporary:
        jdk = Path(temporary) / "jdk"
        _extract_jdk(bazel, jdk)
        shutil.copyfile(jdk / "lib/security/cacerts", store)
        for path in paths:
            _run([jdk / "bin/keytool", "-importcert", "-noprompt", "-trustcacerts",
                  "-alias", "sonic-build-trust-" + path.stem, "-file", path,
                  "-keystore", store, "-storepass", "changeit"])
    # These bytes are public CA certificates; make stores readable to the
    # unprivileged native/Bazel worker accounts regardless of the caller's umask.
    for directory in (trust, certs):
        directory.chmod(0o755)
    for path in [trust / "ca-bundle.pem", store, *paths]:
        path.chmod(0o644)
    rc = root / "etc/bazel.bazelrc"
    original = rc.read_text() if rc.exists() else ""
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(original.rstrip() + "\n# Explicit execution-only CA input.\n"
                  "startup --host_jvm_args=-Djavax.net.ssl.trustStore=/" +
                  str(TRUST_DIRECTORY / "java-cacerts") + "\n")
    rc.chmod(0o644)
    receipt = trust / "receipt.json"
    receipt.write_text(json.dumps(metadata, sort_keys=True) + "\n")
    receipt.chmod(0o644)
    return metadata


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "stage":
        parser = argparse.ArgumentParser(description="Stage validated CA certificates without publishing their contents")
        parser.add_argument("--source", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--certificates-output", type=Path,
                            help="also stage individual .crt files for a worker build context")
        args = parser.parse_args(argv[1:])
        try:
            result = stage_bundle(args.source, args.output, args.certificates_output)
        except (ValueError, OSError) as error:
            parser.exit(1, str(error) + "\n")
        print(json.dumps(result, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--bazel", default="/usr/local/bin/bazel", type=Path)
    parser.add_argument("--validate", action="store_true", help="validate without installing")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            result = validate_bundle(args.bundle)
        else:
            if os.geteuid() != 0:
                raise ValueError("trust installation requires the disposable worker's root user")
            result = install_bundle(args.bundle, args.bazel)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
