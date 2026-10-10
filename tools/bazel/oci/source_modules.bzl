"""Expose resolved owner module declarations as inputs to payload validation.

Some owners do not export MODULE.bazel as a build target. Repository rules can
read those declared labels without changing the owners or copying their source.
"""

def _source_modules_impl(repository_ctx):
    names = []
    for source, module in repository_ctx.attr.modules.items():
        if not module or "/" in module or "\\" in module or module in names:
            fail("expected unique module names without path separators")
        names.append(module)
        repository_ctx.file(module + ".MODULE.bazel", repository_ctx.read(source), executable = False)
    repository_ctx.file("BUILD.bazel", "exports_files(%s)\n" % json.encode([
        name + ".MODULE.bazel"
        for name in names
    ]))

source_modules = repository_rule(
    implementation = _source_modules_impl,
    attrs = {"modules": attr.label_keyed_string_dict(mandatory = True)},
)
