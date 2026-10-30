#!/usr/bin/env python3
"""Configure an offline SONiC host using its installed package-manager APIs.

The caller supplies verified OCI labels and rootfs trees unpacked by umoci.
Only package registration and host CLI files are installed here. The preloaded store
contains the corresponding OCI images; this helper never creates a Docker
client or substitutes a Docker API implementation.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil


def copy_cli_plugins(package, rootfs, commands, plugin_path):
    """Copy manifest-selected files, retaining the package manager's names."""
    rootfs = Path(rootfs).resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', package.name) or package.name in ('.', '..'):
        raise ValueError('unsafe package name for CLI plugins')
    for command in commands:
        for index, image_path in enumerate(package.manifest['cli'][command]):
            path = PurePosixPath(image_path)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('CLI plugin must use an absolute image path without traversal')
            source = (rootfs / str(path).lstrip('/')).resolve(strict=True)
            if not source.is_relative_to(rootfs) or not source.is_file():
                raise ValueError('CLI plugin escapes the unpacked image or is not a file')
            destination = Path(plugin_path(package, index, command))
            if destination.is_symlink():
                raise ValueError('host CLI plugin destination must not be a symlink')
            shutil.copy2(source, destination)


def register(plan):
    # These imports deliberately run inside the restored SONiC root. Reuse its
    # manifest defaults, validators, service templates, feature registry, YANG
    # installation and persistent initial-config handling without get_manager().
    from config import config_mgmt
    from sonic_cli_gen.generator import CliGenerator
    from sonic_py_common import device_info
    from sonic_package_manager.database import PackageDatabase, PackageEntry
    from sonic_package_manager.logger import log
    from sonic_package_manager.manager import (
        get_cli_plugin_path, validate_package_base_os_constraints,
        validate_package_cli_can_be_skipped, validate_package_tree,
    )
    from sonic_package_manager.metadata import MetadataResolver
    from sonic_package_manager.package import Package
    from sonic_package_manager.service_creator import SONIC_CLI_COMMANDS
    from sonic_package_manager.service_creator.creator import ServiceCreator
    from sonic_package_manager.service_creator.feature import FeatureRegistry
    from sonic_package_manager.service_creator.sonic_db import INIT_CFG_JSON, SonicDB
    from sonic_package_manager.service_creator.utils import in_chroot

    if not in_chroot():
        raise ValueError('offline registration must run inside the isolated SONiC chroot')
    if plan.get('schema') != 1 or set(plan) != {'schema', 'builtin', 'local'}:
        raise ValueError('unsupported offline host registration schema')
    database = PackageDatabase.from_file()
    creator = ServiceCreator(FeatureRegistry(SonicDB), SonicDB,
                             CliGenerator(log), config_mgmt.ConfigMgmt(source=INIT_CFG_JSON))
    packages = {}
    for record in plan['builtin']:
        name = record['name']
        if name in packages:
            raise ValueError('duplicate built-in package metadata: ' + name)
        entry = database.get_package(name)
        if not entry.installed or not entry.built_in:
            raise ValueError('expected an installed built-in package: ' + name)
        packages[name] = Package(entry, MetadataResolver.from_labels(record['metadata']['labels']))
    if set(packages) != {entry.name for entry in database if entry.installed}:
        raise ValueError('built-in metadata does not cover the installed package database')
    version_info = device_info.get_sonic_version_info()
    # This mirrors PackageManager.install_from_source after image loading, using
    # the manifest digest imported into the preloaded containerd store.
    for record in plan['local']:
        metadata = MetadataResolver.from_labels(record['metadata']['labels'])
        name = metadata.manifest['package']['name']
        if name in packages or database.has_package(name):
            raise ValueError('local package conflicts with an existing package: ' + name)
        reference = record['reference']
        if not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*:[A-Za-z0-9_.-]+', reference):
            raise ValueError('invalid local package image reference')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', record['image_id']):
            raise ValueError('local package requires an OCI manifest digest')
        entry = PackageEntry(name, reference.rsplit(':', 1)[0],
                             description=metadata.manifest['package']['description'],
                             image_id=record['image_id'], docker_image_reference=reference)
        package = Package(entry, metadata)
        candidate = dict(packages, **{name: package})
        validate_package_base_os_constraints(package, version_info)
        validate_package_tree(candidate)
        validate_package_cli_can_be_skipped(package, False)
        creator.create(package, state='enabled' if record['enabled'] else 'disabled',
                       owner=record['owner'])
        copy_cli_plugins(package, record['rootfs'], SONIC_CLI_COMMANDS, get_cli_plugin_path)
        entry.installed = True
        entry.version = metadata.manifest['package']['version']
        database.add_package(name, entry.repository)
        database.update_package(entry)
        packages[name] = package
    creator.generate_shutdown_sequence_files(packages)
    database.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    args = parser.parse_args()
    register(json.loads(Path(args.input).read_text()))


if __name__ == '__main__':
    main()
