#!/usr/bin/env python3
"""Render SWSS's build-time templates from its declared Make metadata.

The runtime templates remain unrendered until docker-init runs sonic-cfggen with
the switch database. This renderer covers the non-ASAN container configuration.
"""

import argparse
import json
from pathlib import Path
import re

import jinja2


# These names mirror rules/functions:generate_manifest, not _RUN_OPT. The latter
# controls docker run and is not the package manifest's container configuration.
MANIFEST_FIELDS = {
    "VERSION": "version",
    "CONTAINER_NAME": "name",
    "PACKAGE_NAME": "package_name",
    "PACKAGE_DEPENDS": "depends",
    "SERVICE_REQUIRES": "requires",
    "SERVICE_AFTER": "after",
    "SERVICE_BEFORE": "before",
    "SERVICE_DEPENDENT_OF": "dependent_of",
    "WARM_SHUTDOWN_AFTER": "warm_shutdown_after",
    "WARM_SHUTDOWN_BEFORE": "warm_shutdown_before",
    "FAST_SHUTDOWN_AFTER": "fast_shutdown_after",
    "FAST_SHUTDOWN_BEFORE": "fast_shutdown_before",
    "CONTAINER_PRIVILEGED": "privileged",
    "CONTAINER_VOLUMES": "volumes",
    "CONTAINER_TMPFS": "tmpfs",
    "CLI_CONFIG_PLUGIN": "config_cli_plugin",
    "CLI_SHOW_PLUGIN": "show_cli_plugin",
    "CLI_CLEAR_PLUGIN": "clear_cli_plugin",
    "SUPPORT_RATE_LIMIT": "support_rate_limit",
}


def manifest_metadata(make_source):
    """Read this recipe's literal manifest assignments, rejecting new Make logic.

    This is deliberately not a Make evaluator. If a field becomes conditional or
    computed, fail so that the Bazel configuration must model that input too.
    """
    context = {name: "" for name in MANIFEST_FIELDS.values()}
    condition_depth = 0
    assignment = re.compile(r"^\$\(DOCKER_ORCHAGENT\)_([A-Z_]+)\s*([:+?]?=)\s*(.*?)\s*$")
    for line in make_source.splitlines():
        line = line.split("#", 1)[0].strip()
        if re.match(r"^(ifeq|ifneq|ifdef|ifndef)\b", line):
            condition_depth += 1
        elif line == "endif":
            condition_depth -= 1
        match = assignment.match(line)
        if not match or match[1] not in MANIFEST_FIELDS:
            continue
        key, operator, value = match.groups()
        if condition_depth or "$" in value or value.endswith("\\"):
            raise ValueError(f"Computed or conditional SWSS manifest field requires a declared input: {key}")
        name = MANIFEST_FIELDS[key]
        if operator == "+=":
            context[name] = (context[name] + " " + value).strip()
        elif operator in ("=", ":="):
            context[name] = value
        else:
            raise ValueError(f"Unsupported SWSS manifest assignment: {line}")
    for key in ("version", "name", "package_name"):
        if not context[key]:
            raise ValueError(f"Missing required SWSS manifest field: {key}")
    return context


def render_manifest(template, context, debug=False):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["json"] = json.dumps
    values = dict(context, version_suffix="dbg" if debug else "")
    # Parse the result before publishing so malformed metadata fails the action.
    return json.loads(env.from_string(template).render(values))


def render_init(template):
    # Match sonic-cfggen's trim_blocks=True environment and final print newline.
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    return env.from_string(template).render(ENABLE_ASAN="n") + "\n"


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
    context = manifest_metadata(args.make_metadata.read_text())
    # SWSS has a per-namespace service template, with no host service or manifest
    # overlay in this source revision. The service file is a declared dependency.
    if not args.asic_service_template.is_file():
        raise ValueError("SWSS's per-namespace service template is missing")
    context.update(asic_service="true", host_service="false")
    template = args.manifest_template.read_text()
    for debug, manifest_path, labels_path in (
        (False, args.manifest_output, args.labels_output),
        (True, args.debug_manifest_output, args.debug_labels_output),
    ):
        manifest = render_manifest(template, context, debug)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        labels_path.write_text(
            "com.azure.sonic.manifest=" + json.dumps(manifest, separators=(",", ":")) + "\n"
        )
    args.init_output.write_text(render_init(args.docker_init_template.read_text()))


if __name__ == "__main__":
    main()
