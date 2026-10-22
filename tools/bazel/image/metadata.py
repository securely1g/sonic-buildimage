#!/usr/bin/env python3
"""Project service metadata independently of OCI filesystem layers.

The JSON is a declared host-build input. The optional zero-layer Docker archive
is only for a throwaway host-configuration daemon; it must never be shipped.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

MARKER = 'org.sonic.bazel.host-metadata-only'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def project(config):
    if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
        raise ValueError('host metadata currently supports Linux amd64 images only')
    labels = config.get('config', {}).get('Labels') or {}
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError('OCI config labels must be strings')
    if MARKER in labels:
        raise ValueError('cannot project an already metadata-only image')
    manifest = json.loads(labels.get('com.azure.sonic.manifest', 'null'))
    if not isinstance(manifest, dict):
        raise ValueError('SONiC service manifest is missing or invalid')
    return {'schema': 1, 'purpose': 'host-configuration-only', 'architecture': 'amd64', 'os': 'linux', 'labels': labels}


def from_archive(path):
    # Stream Docker save archives rather than materializing their filesystem
    # layers. Manifests may occur after the config, so retain only JSON members.
    configs = {}
    manifest = None
    with tarfile.open(path, 'r|*') as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name.removeprefix('./')
            if name == 'manifest.json':
                if member.size > 16 * 1024 * 1024:
                    raise ValueError('oversized Docker manifest')
                manifest = json.load(archive.extractfile(member))
            elif member.size <= 16 * 1024 * 1024:
                # Docker 28 save may name config blobs by digest without a
                # .json suffix. Inspect small JSON-looking members, retaining
                # only actual image configs, never filesystem layer contents.
                stream = archive.extractfile(member)
                prefix = stream.read(min(512, member.size))
                if not prefix.lstrip().startswith(b'{'):
                    continue
                try:
                    value = json.loads(prefix + stream.read())
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict) and 'architecture' in value and 'rootfs' in value:
                    configs[name] = value
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError('expected one image in a Docker save archive')
    config_name = manifest[0].get('Config', '')
    if config_name not in configs:
        raise ValueError('Docker config is missing')
    return project(configs[config_name])


def from_oci(path):
    root = Path(path)
    index = json.loads((root / 'index.json').read_text())
    manifests = index.get('manifests', [])
    if len(manifests) != 1:
        raise ValueError('expected one image in OCI layout')
    def blob(descriptor):
        digest = descriptor['digest']
        if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith('sha256:') or any(c not in '0123456789abcdef' for c in digest[7:]):
            raise ValueError('invalid OCI digest')
        data = (root / 'blobs/sha256' / digest[7:]).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest[7:]:
            raise ValueError('OCI blob digest mismatch')
        return json.loads(data)
    return project(blob(blob(manifests[0])['config']))


def staging_archive(metadata, name, output):
    if metadata.get('schema') != 1 or metadata.get('purpose') != 'host-configuration-only':
        raise ValueError('invalid host metadata projection')
    if not name or PurePosixPath(name).name != name or not name.endswith('.gz') or ':' in name:
        raise ValueError('expected a Docker archive basename ending in .gz')
    # Validate again using the exact source labels; the marker is added only to
    # the temporary daemon image and never becomes part of the source projection.
    projection = project({'architecture': metadata['architecture'], 'os': metadata['os'], 'config': {'Labels': metadata['labels']}})
    labels = dict(projection['labels'])
    labels[MARKER] = 'true'
    image_config = {'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': labels}, 'rootfs': {'type': 'layers', 'diff_ids': []}, 'history': []}
    config_data = canonical(image_config)
    config_name = hashlib.sha256(config_data).hexdigest() + '.json'
    manifest = [{'Config': config_name, 'RepoTags': [name.removesuffix('.gz') + ':latest'], 'Layers': []}]
    with tarfile.open(output, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for path, data in ((config_name, config_data), ('manifest.json', canonical(manifest))):
            member = tarfile.TarInfo(path)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive')
    source.add_argument('--oci')
    source.add_argument('--config')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = from_archive(args.archive) if args.archive else from_oci(args.oci) if args.oci else project(json.loads(Path(args.config).read_text()))
    Path(args.output).write_bytes(canonical(result))


if __name__ == '__main__':
    main()
