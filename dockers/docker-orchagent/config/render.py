#!/usr/bin/env python3
"""Render SWSS's build-time templates from its declared Make metadata.

The runtime templates remain unrendered until docker-init runs sonic-cfggen with
the switch database. This renderer covers the non-ASAN container configuration.
"""

import argparse
import json
from pathlib import Path

import container_config


def manifest_context(make_source):
    """Supply SWSS's Make metadata and service scope to the shared renderer."""
    context = container_config.manifest_metadata(make_source, "DOCKER_ORCHAGENT")
    context.update(asic_service="true", host_service="false")
    return context


def render_init(template):
    return container_config.render_template(template, {"ENABLE_ASAN": "n"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--make-metadata", type=Path, required=True)
    parser.add_argument("--manifest-template", type=Path, required=True)
    parser.add_argument("--asic-service-template", type=Path, required=True)
    parser.add_argument("--docker-init-template", type=Path, required=True)
    parser.add_argument("--init-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--debug-manifest-output", type=Path, required=True)
    parser.add_argument("--labels-output", type=Path, required=True)
    parser.add_argument("--debug-labels-output", type=Path, required=True)
    args = parser.parse_args()
    context = manifest_context(args.make_metadata.read_text())
    # SWSS has a per-namespace service template, with no host service or manifest
    # overlay in this source revision. The service file is a declared dependency.
    if not args.asic_service_template.is_file():
        raise ValueError("SWSS's per-namespace service template is missing")
    template = args.manifest_template.read_text()
    for debug, manifest_path, labels_path in (
        (False, args.manifest_output, args.labels_output),
        (True, args.debug_manifest_output, args.debug_labels_output),
    ):
        manifest = container_config.render_manifest(
            template, context, version_suffix="dbg" if debug else ""
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        labels_path.write_text(container_config.manifest_label(manifest))
    args.init_output.write_text(render_init(args.docker_init_template.read_text()))


if __name__ == "__main__":
    main()
