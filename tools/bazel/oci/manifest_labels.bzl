"""Convert one declared Make manifest into an OCI label file."""

load("@aspect_bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")

def manifest_labels(name, manifest, out = None, **kwargs):
    """Convert JSON without re-evaluating Make metadata or manifest templates.

    Runtime and debug manifests use separate calls so changing either input
    invalidates only its label action. Make must prepare the source manifest
    before Bazel runs.
    """
    out = out or name + ".labels"
    run_binary(
        name = name,
        srcs = [manifest],
        outs = [out],
        args = [
            "--manifest",
            shell.quote("$(location %s)" % manifest),
            "--output",
            shell.quote("$(location :%s)" % out),
        ],
        tool = Label("//tools/bazel/oci:manifest_labels"),
        mnemonic = "ContainerManifestLabel",
        **kwargs
    )
