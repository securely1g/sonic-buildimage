"""Expose an archive built with the Make owner's configuration to a multi-target build."""

def _configure(settings, attr):
    result = {}
    for key, value in attr.settings.items():
        current = settings[key]
        if type(current) == "bool":
            values = {"true": True, "1": True, "false": False, "0": False}
            if value.lower() not in values:
                fail("Invalid boolean for %s: %s" % (key, value))
            result[key] = values[value.lower()]
        elif type(current) == "Label":
            result[key] = Label(value)
        elif type(current) == "string":
            result[key] = value
        else:
            fail("Unsupported container build setting %s of type %s" % (key, type(current)))
    return result

def _archive(ctx):
    files = ctx.files.archive
    if len(files) != 1:
        fail("%s must produce exactly one archive, got %s" % (ctx.attr.archive, files))
    return [DefaultInfo(files = depset(files))]

def configured_archive_rule(keys):
    """Create a forwarding rule that changes only this owner's build settings."""
    return rule(
        implementation = _archive,
        attrs = {
            "archive": attr.label(
                mandatory = True,
                allow_files = True,
                cfg = transition(implementation = _configure, inputs = keys, outputs = keys),
            ),
            "settings": attr.string_dict(mandatory = True),
            "_allowlist_function_transition": attr.label(
                default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
            ),
        },
    )
