#!/usr/bin/env python3
"""Select a store graph after strict native-input preparation has completed."""

import ast
import json
import os
from pathlib import Path
import pprint
import re
import tempfile

KEY = "use_container_image_store_as_backend"
BUILD_LINE = '    use_container_image_store_as_backend = IMAGE_INPUTS.get("use_container_image_store_as_backend", False),\n'


def selection(build_with_bazel_when_available, use_container_image_store_as_backend):
    for name, value in (("BUILD_WITH_BAZEL_WHEN_AVAILABLE", build_with_bazel_when_available),
                        ("USE_CONTAINER_IMAGE_STORE_AS_BACKEND", use_container_image_store_as_backend)):
        if value not in ("y", "n"):
            raise ValueError(name + " must be y or n")
    if use_container_image_store_as_backend == "y" and build_with_bazel_when_available != "y":
        raise ValueError("USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y requires BUILD_WITH_BAZEL_WHEN_AVAILABLE=y")
    return use_container_image_store_as_backend == "y"


def write_text(path, content):
    """Replace generated metadata atomically without changing unchanged files."""
    if path.is_symlink():
        raise ValueError("refusing to replace linked image metadata: " + str(path))
    if path.exists() and path.read_text() == content:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="." + path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def select_backend(workspace, *, build_with_bazel_when_available, use_container_image_store_as_backend):
    """Keep all prepared bytes and producer identities while selecting a graph.

    The caller holds the final-image publication lock. This does not create or
    adopt a native bundle: only prepare_inputs.py verifies and stages one.
    """
    enabled = selection(build_with_bazel_when_available, use_container_image_store_as_backend)
    workspace = Path(workspace).resolve(strict=True)
    output = workspace / "target/bazel-image-inputs"
    package = workspace / "tools/bazel/image/vs"
    paths = [output, package, output / "inputs.bzl", output / "provenance.json", package / "BUILD.bazel.in"]
    if any(path.resolve(strict=True) != path for path in paths):
        raise ValueError("prepared image metadata must not be linked")
    template = (package / "BUILD.bazel.in").read_text()
    if template.count(BUILD_LINE) != 1:
        raise ValueError("VS BUILD template does not declare the image store selector")
    build_file = package / "BUILD.bazel"
    if (build_file.is_symlink() or not build_file.is_file()
            or build_file.read_text() not in (template, template.replace(BUILD_LINE, ""))):
        raise ValueError("refusing to replace a customized or missing VS BUILD.bazel")
    inputs = output / "inputs.bzl"
    provenance = output / "provenance.json"
    tree = ast.parse(inputs.read_text())
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign)
            or len(tree.body[0].targets) != 1 or not isinstance(tree.body[0].targets[0], ast.Name)
            or tree.body[0].targets[0].id != "IMAGE_INPUTS"):
        raise ValueError("expected a literal IMAGE_INPUTS assignment")
    attributes = ast.literal_eval(tree.body[0].value)
    if not isinstance(attributes, dict):
        raise ValueError("IMAGE_INPUTS must be a dictionary")
    receipt = json.loads(provenance.read_text())
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or not isinstance(receipt.get("files"), dict)
            or not isinstance(receipt.get("native_provenance_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["native_provenance_sha256"])
            or any(not isinstance(receipt.get(key), dict) for key in
                   ("native_source_identity", "assembler_source_identity", "native_config_flags"))):
        raise ValueError("verified native-input provenance is required; run prepare_inputs.py")
    for value in (attributes.get(KEY, False), receipt.get(KEY, False)):
        if type(value) is not bool:
            raise ValueError("recorded image store selection must be a boolean")
    attributes[KEY] = enabled
    receipt[KEY] = enabled
    # Python literals retain Starlark booleans; JSON's true/false are invalid.
    write_text(inputs, "# Generated from verified native predecessors.\nIMAGE_INPUTS = "
               + pprint.pformat(attributes, sort_dicts=True, width=100) + "\n")
    write_text(provenance, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    write_text(build_file, template)
    return enabled
