#!/usr/bin/env python3
"""Carry verified, pinned build-tool source downloads from hosted CI to VS jobs."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import stat
import tempfile


# Registry archives retain their source.json integrity and upstream filename:
# https://bcr.bazel.build/modules/<module>/<version>/source.json
# Bison 3.3.2 and M4 1.4.18 are extension downloads, not BCR module versions.
# Their selected owner modules pin the default version, filename and SHA256 in:
# https://github.com/jmillikin/rules_bison/blob/v0.4/bison/internal/versions.bzl
# https://github.com/jmillikin/rules_m4/blob/v0.3.1/m4/internal/versions.bzl
# Keep each archive in a separate distdir: the GitLab sources share src.tar.gz.
SOURCES = {
    'ape-1.0.1': {
        'module': 'ape',
        'version': '1.0.1',
        'repository': '@@ape+',
        'filename': 'src.tar.gz',
        'integrity': 'sha512-YHlfxYHiCxzrifn5ywADS59XhmTk5u0rW/1fPepfk1C6/bXn3uwSADixLkvee6rAP9u2sulmHX0igt5F5+eqTA==',
    },
    'download_utils-1.0.1': {
        'module': 'download_utils',
        'version': '1.0.1',
        'repository': '@@download_utils+',
        'filename': 'src.tar.gz',
        'integrity': 'sha512-nsUJrJJjUWKzTQA187uWHhnBSqPA7t8Q6Rqry83Sj63Kf09L6xEFCR7thlnRUtbRbzabIKGr7JreouFdjjdarw==',
    },
    'rules_coreutils-1.0.1': {
        'module': 'rules_coreutils',
        'version': '1.0.1',
        'repository': '@@rules_coreutils+',
        'filename': 'src.tar.gz',
        'integrity': 'sha512-ViXfJnpIYJ/rhVAcq/WIc/SwekMKubAFS/1cVrziq9NoHkLBtXbf977l/fEknU1ElXtteVDtxOfgi8vcM+34Qg==',
    },
    'rules_gzip-1.0.0': {
        'module': 'rules_gzip',
        'version': '1.0.0',
        'repository': '@@rules_gzip+',
        'filename': 'src.tar.gz',
        'integrity': 'sha512-KK1A/NSvOaixXftdIRSBHt+QSPEk1ZorO2HtKY6o5KRP/i8KQ1hsDQPVwbCCi0++6pE529idMytTL/3SmhNmCQ==',
    },
    'toolchain_utils-1.0.2': {
        'module': 'toolchain_utils',
        'version': '1.0.2',
        'repository': '@@toolchain_utils+',
        'filename': 'src.tar.gz',
        'integrity': 'sha512-AXqtA9CzczVUdS5dw0dX332eaXZQhFcEZmf1iKALwsoKoT9Mboah7/j1JN9hZpzq6B5X7gpe83TPpya6yJZ6Lw==',
    },
    'pigz-2.8.bcr.2': {
        'module': 'pigz',
        'version': '2.8.bcr.2',
        'repository': '@@pigz+',
        'filename': 'pigz-2.8.tar.gz',
        'integrity': 'sha256-64crTw4fDr5Zyfe9jFBsQgSJO6aoSS3jHfQW8NUXD9A=',
    },
    'gawk-5.3.2.bcr.3': {
        'module': 'gawk',
        'version': '5.3.2.bcr.3',
        'repository': '@@gawk+',
        'filename': 'gawk-5.3.2.tar.xz',
        'integrity': 'sha256-+MNIZQnecFGSE4sA7ywAu73Q6Eww1cB9I/xzqdxMycw=',
    },
    'bison-3.3.2': {
        'module': 'rules_bison',
        'version': '0.4',
        'repository': '@@rules_bison++default_toolchain_ext+bison_v3.3.2',
        'filename': 'bison-3.3.2.tar.xz',
        'integrity': 'sha256-A57kW2HZXlAD5+g3b5CAABtAZv81e94nG3+qzlO52AQ=',
    },
    'm4-1.4.18': {
        'module': 'rules_m4',
        'version': '0.3.1',
        'repository': '@@rules_m4++default_toolchain_ext+m4_v1.4.18',
        'filename': 'm4-1.4.18.tar.xz',
        'integrity': 'sha256-8sHobKCkBP8oFjG9yDd2OJknRLF1r7gG4lhxokqTTgc=',
    },
    'm4-1.4.21.bcr.4': {
        'module': 'm4',
        'version': '1.4.21.bcr.4',
        'repository': '@@m4+',
        'filename': 'm4-1.4.21.tar.xz',
        'integrity': 'sha256-8lxqtRVIpzp1VYdC+wMeBiXWSF/l+RVZSdZIaiQIq2Y=',
    },
    'make-4.4.1.bcr.1': {
        'module': 'make',
        'version': '4.4.1.bcr.1',
        'repository': '@@make+',
        'filename': 'make-4.4.1.tar.gz',
        'integrity': 'sha256-3Rb7HWe/q3mnL16DkHNcSePo5wtJRaFasfgd23hlj7M=',
    },
}


def check_versions(module_graph):
    """Bind each source to its selected module, including extension owners."""
    selected = {}

    def visit(module):
        selected.setdefault(module["name"], set()).add(module["version"])
        for dependency in module.get("dependencies", []):
            visit(dependency)

    visit(json.loads(module_graph.read_text()))
    for source in SOURCES.values():
        name, version = source["module"], source["version"]
        if selected.get(name) != {version}:
            raise ValueError(f"{name} selection changed; refresh its source-download integrity")


def archive_digest(source):
    """Decode only the reviewed hash algorithms used by these source pins."""
    algorithm, encoded = source["integrity"].split("-", 1)
    if algorithm not in ("sha256", "sha512"):
        raise ValueError("unsupported source archive integrity algorithm")
    digest = base64.b64decode(encoded, validate=True)
    if len(digest) != hashlib.new(algorithm).digest_size:
        raise ValueError("invalid source archive integrity length")
    return algorithm, digest.hex()


def archive_path(name, source):
    """Keep the reviewed archive name inside its own distdir."""
    for component in (name, source["filename"]):
        if not component or component in (".", "..") or Path(component).name != component:
            raise ValueError("invalid source archive path")
    return Path(name) / source["filename"]


def read_archive(root, relative):
    """Refuse missing, linked or non-regular inputs before reading any bytes."""
    path = root
    components = (None, *relative.parts)
    for index, component in enumerate(components):
        if component is not None:
            path /= component
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise ValueError(f"missing source archive input: {path}") from error
        directory = index != len(components) - 1
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError(f"source archive input must be an ordinary {'directory' if directory else 'file'}: {path}")
    return path.read_bytes()


def verified_archives(root, *, repository_cache=False):
    """Verify the complete manifest before callers publish or pass any input."""
    verified = []
    for name, source in SOURCES.items():
        algorithm, digest = archive_digest(source)
        relative = archive_path(name, source)
        cached = Path("content_addressable") / algorithm / digest / "file"
        data = read_archive(Path(root), cached if repository_cache else relative)
        if hashlib.new(algorithm, data).hexdigest() != digest:
            raise ValueError(f"{name} source archive does not match its pinned {algorithm.upper()}")
        verified.append((relative, data, source))
    return verified


def fetch_args():
    """Fetch only reviewed repositories; do not evaluate unrelated extensions."""
    return ["--repo=" + source["repository"] for source in SOURCES.values()]


def distdir_args(root, *, prefix=None):
    """Return verified distdirs, optionally mapped into the native container."""
    verified = verified_archives(root)
    directory = Path(root if prefix is None else prefix)
    return ["--distdir=" + str(directory / relative.parent) for relative, _, _ in verified]


def publish(data, destination):
    """Atomically install an already verified archive."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repository-cache", type=Path)
    source.add_argument("--archives-dir", type=Path)
    source.add_argument("--fetch-arguments", action="store_true")
    source.add_argument("--distdir-arguments", type=Path)
    parser.add_argument("--distdir-prefix", type=Path)
    parser.add_argument("--module-graph", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.distdir_prefix is not None and args.distdir_arguments is None:
        parser.error("--distdir-prefix requires --distdir-arguments")
    if args.fetch_arguments or args.distdir_arguments is not None:
        if args.output is not None or args.module_graph is not None:
            parser.error("argument listing does not accept --output or --module-graph")
        arguments = fetch_args() if args.fetch_arguments else distdir_args(
            args.distdir_arguments, prefix=args.distdir_prefix)
        if any("\n" in argument or "\r" in argument for argument in arguments):
            raise ValueError("source archive arguments must not contain newlines")
        print("\n".join(arguments))
        return
    if args.output is None:
        parser.error("archive export/import requires --output")
    if args.repository_cache is not None:
        if args.module_graph is None:
            parser.error("--repository-cache requires --module-graph")
        check_versions(args.module_graph)
    verified = verified_archives(
        args.repository_cache if args.repository_cache is not None else args.archives_dir,
        repository_cache=args.repository_cache is not None)
    # Verify the entire set before replacing any existing archive.
    receipts = []
    for relative, data, source in verified:
        destination = args.output / relative
        publish(data, destination)
        receipts.append({"module": source["module"], "version": source["version"],
                         "repository": source["repository"], "integrity": source["integrity"],
                         "bytes": len(data), "archive": str(destination)})
    print(json.dumps(receipts, indent=2))


if __name__ == "__main__":
    main()
