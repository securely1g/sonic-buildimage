#!/usr/bin/env python3
"""Prepare native CI APT transport before the chroot has Python or CA trust."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
from urllib.parse import urlsplit


HELPER = Path(__file__).resolve().parents[1] / "src/sonic-build-hooks/scripts/native_apt_transport.py"
SPEC = importlib.util.spec_from_file_location("native_apt_transport", HELPER)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
VIEW = Path("var/lib/sonic-native-apt")
TRUST = Path("usr/share/sonic-native-apt")
CONFIG = Path("etc/apt/apt.conf.d/99sonic-native-apt")
CONFIG_BASE = (
    'Dir::Etc::sourcelist "/' + str(VIEW / "sources.list") + '";\n'
    'Acquire::https::snapshot.debian.org::CaInfo "/' + str(TRUST / "ca-certificates.crt") + '";\n'
)


def distro_ca_bundle():
    # The worker can also trust private host CAs. Only copy enabled public
    # distro roots, never /etc/ssl/certs or /usr/local/share/ca-certificates.
    directory = Path("/usr/share/ca-certificates/mozilla")
    enabled = Path("/etc/ca-certificates.conf").read_text().splitlines()
    certificates = []
    for entry in enabled:
        if re.fullmatch(r"mozilla/[A-Za-z0-9_.-]+\.crt", entry):
            certificate = directory / Path(entry).name
            if certificate.is_symlink() or not certificate.is_file():
                raise ValueError("Enabled distro CA certificate must be a regular file")
            certificates.append(certificate.read_text(encoding="ascii").strip())
    if not certificates:
        raise ValueError("Native rootfs APT requires enabled distro CA certificates")
    bundle = "\n".join(certificates) + "\n"
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=bundle)
    return bundle


def setup(root, stamps):
    if not all(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamps.get(key, ""))
               for key in ("debian", "debian-security")):
        raise ValueError("Native rootfs APT requires the pinned snapshot timestamps")
    source = root / "etc/apt/sources.list"
    view, trust, config = root / VIEW, root / TRUST, root / CONFIG
    if any(path.exists() or path.is_symlink() for path in (view, trust, config)):
        raise ValueError("Native rootfs APT transport is already present")
    proxy = os.environ.get("http_proxy", "")
    parsed = urlsplit(proxy)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "192.168.240.1"} or not parsed.port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Native rootfs APT requires the current worker HTTP adapter")
    https_proxy = os.environ.get("https_proxy", os.environ.get("HTTPS_PROXY", "")) or "DIRECT"
    configuration = CONFIG_BASE + ''.join(
        "Acquire::" + transport + "::Proxy " + json.dumps(value) + ";\n"
        for transport, value in (("http", proxy), ("https", https_proxy)))
    content = native.source_view(source.read_text(), ".list", stamps)
    bundle = distro_ca_bundle()
    view.mkdir(mode=0o700)
    try:
        temporary_source = view / "sources.list"
        temporary_source.write_text(content)
        temporary_source.chmod(stat.S_IMODE(source.stat().st_mode) & 0o644)
        # HTTPS methods can run as _apt; only the public roots are traversable.
        trust.mkdir(mode=0o755)
        trust.chmod(0o755)
        certificates = trust / "ca-certificates.crt"
        certificates.write_text(bundle)
        certificates.chmod(0o644)
        # Leave sourceparts live: later stages add Docker CE and other sources.
        (view / "apt.conf").write_text(configuration)
        (view / "apt.conf").chmod(0o600)
        config.write_text(configuration)
        config.chmod(0o600)
    except BaseException:
        config.unlink(missing_ok=True)
        shutil.rmtree(view)
        if trust.exists():
            shutil.rmtree(trust)
        raise


def cleanup(root):
    view, trust, config = root / VIEW, root / TRUST, root / CONFIG
    if not any(path.exists() or path.is_symlink() for path in (view, trust, config)):
        return
    marker = view / "apt.conf"
    if (any(path.is_symlink() for path in (view, trust, config, marker))
            or not config.is_file() or not marker.is_file()
            or config.read_bytes() != marker.read_bytes()
            or not config.read_text().startswith(CONFIG_BASE)):
        raise ValueError("Refusing to remove an unexpected rootfs APT configuration")
    for directory in (view, trust):
        if directory.exists():
            shutil.rmtree(directory)
    config.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "cleanup"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--debian-snapshot", default="")
    parser.add_argument("--security-snapshot", default="")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if root == Path("/") or not root.is_dir():
        raise ValueError("A separate existing rootfs directory is required")
    if args.command == "setup":
        setup(root, {"debian": args.debian_snapshot, "debian-security": args.security_snapshot})
    else:
        cleanup(root)


if __name__ == "__main__":
    main()
