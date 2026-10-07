"""Import existing Make-built P4 packages; no package producer runs in Bazel."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")

def _packages_impl(ctx):
    lock = json.decode(ctx.read(Label("//tools/bazel/p4:packages.lock.json")))
    for package in lock["packages"]:
        http_file(
            name = package["repository"],
            urls = [package["url"]],
            sha256 = package["sha256"],
            downloaded_file_path = package["filename"],
        )

packages = module_extension(implementation = _packages_impl)
