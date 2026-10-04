"""Package an `oci_image` into the gzipped docker-archive the Make build consumes.

The Make-based build expects each container to land in `target/<name>.gz` as a
gzipped `docker save` archive.

This macro replicates that process in Bazel, creating intermediary targets when necessary.
"""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("//tools/bazel:gzip.bzl", "gzip")

def sonic_docker_archive(name, image, visibility = None):
    """Packages an `oci_image` into `target/<name>`, where the Make build expects it.

    For `name = "docker-orchagent.gz"`, this defines:

    The archive is always tagged `<name without .gz>:latest`,
    as expected by `sonic_debian_extension.j2`.

    This macro generates several intermediate targets, derived from `name`. Here are the useful ones:

    - `:{name}.load`, an `oci_load` tagged `{name}:latest`. Can be run with `bazel run` to load the image into a local registry.
    - `:{name}`, the compressed archive exported by the Make bridge.

    Args:
        name: File name of the archive, including the `.gz` suffix. Must match the
            name the Make build expects to find in `target/`.
        image: The `oci_image` to package.
        visibility: Visibility for the generated targets.
    """
    if not name.endswith(".gz"):
        fail("sonic_docker_archive: name must end in '.gz', got '%s'" % name)

    stem = name[:-len(".gz")]

    oci_load(
        name = stem + ".load",
        image = image,
        repo_tags = [stem + ":latest"],
        visibility = visibility,
    )

    native.filegroup(
        name = stem + ".tar",
        srcs = [stem + ".load"],
        output_group = "tarball",
        visibility = visibility,
    )

    gzip(
        name = name,
        src = stem + ".tar",
        visibility = visibility,
    )
