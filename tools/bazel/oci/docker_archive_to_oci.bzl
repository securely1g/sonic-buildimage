"""Import Make's Docker archives as rules_oci base images with pinned regctl."""

load("@aspect_bazel_lib//lib:run_binary.bzl", "run_binary")

def docker_archive_to_oci_layout(name, src, expected_platform = "", **kwargs):
    """Import an archive and check its platform before consumers add layers.

    Uses rules_oci's tarball_as_base recipe. The small adapter only invokes
    regctl and validates the result; regctl owns archive and blob conversion.
    """
    regctl = Label("//tools/bazel/oci:regctl")
    args = [
        "--regctl",
        "$(execpath %s)" % regctl,
        "--src",
        "$(execpath %s)" % src,
        "--out",
        "$@",
    ]
    if expected_platform:
        args += ["--expected-platform", expected_platform]

    run_binary(
        name = name,
        srcs = [src, regctl],
        args = args,
        out_dirs = [name + "_layout"],
        tool = Label("//tools/bazel/oci:import_docker_archive"),
        mnemonic = "DockerArchiveToOci",
        progress_message = "Importing %{label} with regctl",
        **kwargs
    )
