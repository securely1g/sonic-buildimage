"""Stage central gNOI schemas for the shared Protobuf source generator."""

_IMPORT_PREFIX = "github.com/openconfig/gnoi"
_PROTOBUF_DEFS = Label("@sonic_protobuf//:defs.bzl")
_PROTOBUF_HEADERS = Label("@sonic_protobuf//:protobuf_headers")
_CC_LIBRARY = Label("@rules_cc//cc:cc_library.bzl")

def _registry_protos_impl(ctx):
    paths = []
    for source, path in ctx.attr.protos.items():
        if path.startswith("/") or ".." in path.split("/") or not path.endswith(".proto"):
            fail("Invalid gNOI schema path: " + path)
        destination = _IMPORT_PREFIX + "/" + path
        ctx.symlink(ctx.path(source), destination)
        paths.append(destination)

    ctx.file("BUILD.bazel", """\
load("{protobuf_defs}", "cpp_proto_sources")
load("{cc_library}", "cc_library")

package(default_visibility = ["//visibility:public"])

cpp_proto_sources(
    name = "generated",
    srcs = {protos},
    output_prefix = "gen",
)

filegroup(name = "sources", srcs = [":generated"], output_group = "sources")
filegroup(name = "headers", srcs = [":generated"], output_group = "headers")
cc_library(
    name = "generated_headers",
    hdrs = [":headers"],
    includes = ["gen", "gen/github.com/openconfig/gnoi"],
    deps = ["{protobuf_headers}"],
)
""".format(
        cc_library = _CC_LIBRARY,
        protobuf_defs = _PROTOBUF_DEFS,
        protobuf_headers = _PROTOBUF_HEADERS,
        protos = repr(paths),
    ))

registry_protos = repository_rule(
    implementation = _registry_protos_impl,
    attrs = {
        "protos": attr.label_keyed_string_dict(allow_files = [".proto"], mandatory = True),
    },
    doc = "Stages explicitly selected BCR schemas with their original import paths.",
)
