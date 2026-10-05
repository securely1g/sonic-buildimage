"""Render SONiC container manifests and build-time templates."""

import json
import re

import jinja2


# Mirror rules/functions:generate_manifest. RUN_OPT controls docker run and is
# separate from the package manifest's container configuration.
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


def manifest_metadata(make_source, make_variable):
    """Read literal manifest assignments for one container's Make variable.

    This is deliberately not a Make evaluator. Selected fields using computed
    values or conditionals require explicit inputs in the caller's build.
    """
    if not isinstance(make_variable, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", make_variable
    ):
        raise ValueError("Container Make variable must be an identifier")
    context = {name: "" for name in MANIFEST_FIELDS.values()}
    condition_depth = 0
    assignment = re.compile(
        r"^\$\(" + re.escape(make_variable) + r"\)_([A-Z_]+)\s*([:+?]?=)\s*(.*?)\s*$"
    )
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
            raise ValueError(
                f"Computed or conditional container manifest field requires a declared input: {key}"
            )
        name = MANIFEST_FIELDS[key]
        if operator == "+=":
            context[name] = (context[name] + " " + value).strip()
        elif operator in ("=", ":="):
            context[name] = value
        else:
            raise ValueError(f"Unsupported container manifest assignment: {line}")
    for key in ("version", "name", "package_name"):
        if not context[key]:
            raise ValueError(f"Missing required container manifest field: {key}")
    return context


def render_manifest(template, context, version_suffix=""):
    """Render a manifest and validate its JSON before publishing any output."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["json"] = json.dumps
    values = dict(context, version_suffix=version_suffix)
    manifest = json.loads(env.from_string(template).render(values))
    if not isinstance(manifest, dict):
        raise ValueError("Container manifest must be a JSON object")
    return manifest


def render_template(template, context):
    """Match sonic-cfggen's block trimming and final print newline."""
    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    return env.from_string(template).render(context) + "\n"


def manifest_label(manifest):
    """Serialize the manifest as the label consumed by SONiC image tooling."""
    return "com.azure.sonic.manifest=" + json.dumps(manifest, separators=(",", ":")) + "\n"
