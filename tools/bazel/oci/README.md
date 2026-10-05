# Shared container configuration rendering

`//tools/bazel/oci:container_config` is a Python library for SONiC container
recipes. It reads literal manifest metadata from Make rules, renders manifests
and build-time Jinja templates, and serializes the SONiC manifest image label.
It has no container name, service scope, feature settings or output paths of
its own. Jinja dependencies are pinned in this package's `requirements_lock.txt`.

Each container keeps its inputs and settings beside its `BUILD.bazel`. The
orchagent example is `dockers/docker-orchagent/config/render.py`: that adapter
selects `DOCKER_ORCHAGENT`, declares ASIC rather than host service scope, disables
ASAN, and writes the five files consumed by its image recipe.

## Use from another container

Add `//tools/bazel/oci:container_config` to the container's `py_binary.deps`,
and pass its Make file and templates as declared inputs to the rendering action.
For example, an adapter for a host service could use:

```python
import container_config

context = container_config.manifest_metadata(make_source, "DOCKER_EXAMPLE")
context.update(asic_service="false", host_service="true")
manifest = container_config.render_manifest(manifest_template, context)
label = container_config.manifest_label(manifest)
startup = container_config.render_template(startup_template, {"FEATURE_ENABLED": "n"})
```

The caller supplies the strings, writes the returned outputs and selects any
version suffix, such as `version_suffix="dbg"`. The manifest template determines
how that suffix is added to the version. Templates use strict undefined-variable
checks; manifest rendering also checks that the result is a JSON object.
Plain template rendering matches `sonic-cfggen`'s block trimming and trailing
newline. Manifest labels contain compact JSON under `com.azure.sonic.manifest`.

The metadata reader supports literal `=`, `:=` and `+=` assignments to the
selected container's manifest fields. It does not evaluate Make: computed,
conditional, multiline or `?=` assignments fail instead of silently selecting
the wrong configuration. Containers needing those features must supply explicit
resolved values through their own declared build inputs. Service discovery,
manifest overlays and runtime configuration remain the caller's responsibility.

Runtime templates that need device or platform state should remain templates
for the container's startup configuration process.

## Validate

```sh
bazel test //tools/bazel/oci:container_config_test \
  //dockers/docker-orchagent/config:render_test
```

The shared tests cover independent container prefixes, supported assignments,
rejected dynamic inputs, strict rendering and label encoding. Orchagent's tests
check its existing manifest and startup behavior and all five CLI outputs.

## Use Make's OCI base

Make publishes `target/docker-config-engine-trixie.oci` alongside its existing
`target/docker-config-engine-trixie.gz`. The pinned Docker 28.5.2 saves both
Docker metadata and an OCI layout in the same archive. Make extracts the existing
OCI files without changing the index, config or layer bytes. Both outputs
therefore describe the same final image, including Make's version-cache cleanup.

The root `BUILD.bazel` declares the layout files as inputs. The
`oci_base_layout` macro validates descriptor digests, sizes and the requested OS
and architecture, then uses upstream `copy_to_directory` to assemble the directory
consumed by `oci_image`. The check reads the image config even when the index has
no platform descriptor. Its validated `oci-layout` marker is a required input to
the copy action. There is no Docker-to-OCI conversion or layer recompression.

Make repairs missing or damaged layouts from the cached archive. It publishes a
complete generation atomically and preserves unchanged files and timestamps on
warm builds. A Docker-only archive from an older cache fails with instructions to
rebuild the base using the pinned Docker version.

Native AMD64 and ARM64 CI exercise the real Make producer and Bazel consumer:

```sh
bazel test //tools/bazel/oci:oci_base_layout_test \
  //tools/bazel/oci:oci_base_consumer_test
```

The fixture starts with a dual-format Docker/OCI archive, publishes its OCI layout
with Make's helper, adds a layer with `oci_image`, and exports through
`sonic_docker_archive`. Tests check image contents, platform rejection and
reproducibility. The consumer fixture uses an AMD64 image on either execution
host. These targets need no existing Make outputs or Docker daemon and create no
Debian packages.

## Reproducible Docker archives

`sonic_docker_archive` uses published `rules_gzip` to compress the Docker-save
tar produced by `oci_load`. Its execution toolchain uses pinned `pigz` with
`--no-name`, configured through standard `rules_multirun` and `toolchain_utils`
rules. Compression stays at level 6; source filenames and timestamps are omitted
from the gzip header. A standard Skylib `copy_file` gives each input the basename
needed to retain Make's `<container>.gz` filename. The intermediate copy lives in
a separate directory so it cannot collide with the OCI image directory.

The `//tools/bazel/oci:sonic_docker_archive_test` fixture exercises this macro and
checks its Docker tag, configuration and layer contents, zero gzip timestamp,
absent source filename, and identical compressed bytes for identical tars with
different source filenames and requested modification times. It reports the
observed input times because Bazel may normalize generated-file metadata.

Native AMD64 and ARM64 jobs run this fixture without Make outputs or a Docker
daemon. To run it on native AMD64 Trixie:

```sh
bazel test //tools/bazel/oci:sonic_docker_archive_test
```

On native ARM64 Trixie, also select
`--platforms=@sonic_build_infra//platforms:aarch64_trixie`. This test validates
compression and archive structure; it does not build a production ARM64 image.
