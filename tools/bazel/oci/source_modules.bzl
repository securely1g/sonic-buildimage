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

def _source_files_impl(repository_ctx):
    names = []
    for source, name in repository_ctx.attr.files.items():
        if not name.endswith(".txt") or "/" in name or "\\" in name or name in names:
            fail("expected unique flat .txt names for source metadata")
        names.append(name)
        repository_ctx.file(name, repository_ctx.read(source), executable = False)
    repository_ctx.file("BUILD.bazel", "exports_files(%s)\n" % json.encode(names))

source_files = repository_rule(
    implementation = _source_files_impl,
    doc = "Expose exact owner text declarations that are not exported as targets.",
    attrs = {"files": attr.label_keyed_string_dict(mandatory = True)},
)
