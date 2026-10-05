#!/usr/bin/env python3
"""Check Make publication, Bazel OCI layering and the legacy Docker export."""

import argparse
import gzip
import io
import json
from pathlib import Path
import tarfile

from oci_base_fixture import OVERLAY_FILES, digest, image_fixture, layer_tar, oci_files


def snapshot(layout):
    return {str(path.relative_to(layout)): path.read_bytes() for path in sorted(layout.rglob("*")) if path.is_file()}


def check_export(path, tag):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(path.read_bytes())), mode="r:") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        assert len(manifest) == 1
        image = manifest[0]
        assert image["RepoTags"] == [tag], "legacy Docker image tag changed"
        assert len(image["Layers"]) == 3, "expected both base layers and the consumer layer"
        config = json.load(archive.extractfile(image["Config"]))
        assert config["os"] == "linux" and config["architecture"] == "amd64"
        assert config["config"]["Env"] == ["BASE_FIXTURE=preserved", "CONSUMER_FIXTURE=added"]
        assert config["config"]["Entrypoint"] == ["/fixture-entrypoint"]
        assert config["config"]["WorkingDir"] == "/fixture-workdir"
        assert config["config"]["Labels"]["sonic.oci.fixture"] == "preserved"
        _, base_layers = image_fixture()
        expected_layers = base_layers + [layer_tar(OVERLAY_FILES)]
        stored_layers = [archive.extractfile(name).read() for name in image["Layers"]]
        actual_layers = [gzip.decompress(layer) if layer.startswith(b"\x1f\x8b") else layer for layer in stored_layers]
        assert actual_layers == expected_layers, "consumer changed layer contents, whiteouts or ordering"
        assert config["rootfs"]["diff_ids"] == [digest(layer) for layer in actual_layers]
        files = {}
        for layer in actual_layers:
            with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as contents:
                for entry in contents:
                    assert entry.isfile(), "fixture must contain only ordinary files"
                    path = Path(entry.name)
                    if path.name.startswith(".wh."):
                        files.pop(str(path.with_name(path.name[4:])), None)
                    else:
                        files[entry.name] = contents.extractfile(entry).read()
        assert files == {"etc/base": b"updated by second layer\n", "etc/second": b"second layer\n", **OVERLAY_FILES}
        return config, actual_layers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-layout", type=Path, required=True)
    parser.add_argument("--second-layout", type=Path, required=True)
    parser.add_argument("--first-image", type=Path, required=True)
    parser.add_argument("--second-image", type=Path, required=True)
    parser.add_argument("--first-export", type=Path, required=True)
    parser.add_argument("--second-export", type=Path, required=True)
    args = parser.parse_args()
    expected = oci_files(*image_fixture())
    assert snapshot(args.first_layout) == expected, "Make or Bazel changed original OCI bytes"
    assert snapshot(args.second_layout) == expected, "outer archive timestamps changed OCI bytes"
    assert snapshot(args.first_image) == snapshot(args.second_image), "OCI consumer output is not reproducible"
    assert check_export(args.first_export, "oci-consumer-first:latest") == check_export(args.second_export, "oci-consumer-second:latest")
    print(json.dumps({"architecture": "amd64", "make_oci_bytes_preserved": True,
                      "consumer_images_identical": True, "base_layers_preserved": 2,
                      "consumer_layers_added": 1, "whiteouts_and_tags_preserved": True}, sort_keys=True))


if __name__ == "__main__":
    main()
