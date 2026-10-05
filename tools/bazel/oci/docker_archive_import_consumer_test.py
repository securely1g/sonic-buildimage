#!/usr/bin/env python3
"""Verify imported bases survive actual rules_oci layering and Docker export."""

import argparse
import gzip
import io
import json
from pathlib import Path
import tarfile

from import_fixture import BASE_FILES, OVERLAY_FILES, SECOND_FILES, digest, image_fixture, layer_tar


def snapshot(layout):
    return {str(path.relative_to(layout)): path.read_bytes() for path in sorted(layout.rglob("*")) if path.is_file()}


def check_export(path, architecture):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(path.read_bytes())), mode="r:") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        assert len(manifest) == 1
        image = manifest[0]
        assert len(image["Layers"]) == 3, "expected both base layers and the consumer layer"
        config = json.load(archive.extractfile(image["Config"]))
        assert config["os"] == "linux" and config["architecture"] == architecture
        assert config["config"]["Env"] == ["BASE_FIXTURE=preserved", "CONSUMER_FIXTURE=added"]
        assert config["config"]["Entrypoint"] == ["/fixture-entrypoint"]
        assert config["config"]["WorkingDir"] == "/fixture-workdir"
        assert config["config"]["Labels"]["sonic.import.fixture"] == "preserved"
        _, base_layers = image_fixture(architecture)
        expected_layers = base_layers + [layer_tar(OVERLAY_FILES)]
        # Docker's loader decompresses each layer before validating its diff_id.
        # rules_oci retains regctl's compressed base layers alongside plain tars.
        stored_layers = [archive.extractfile(name).read() for name in image["Layers"]]
        actual_layers = [gzip.decompress(layer) if layer.startswith(b"\x1f\x8b") else layer for layer in stored_layers]
        assert actual_layers == expected_layers, "consumer changed layer contents or ordering"
        assert config["rootfs"]["diff_ids"] == [digest(layer) for layer in actual_layers]
        files = {}
        for layer in actual_layers:
            with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as contents:
                for entry in contents:
                    assert entry.isfile(), "fixture must contain only ordinary files"
                    files[entry.name] = contents.extractfile(entry).read()
        assert files == {**BASE_FILES, **SECOND_FILES, **OVERLAY_FILES}
        return config, actual_layers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-layout", type=Path, required=True)
    parser.add_argument("--second-layout", type=Path, required=True)
    parser.add_argument("--first-export", type=Path, required=True)
    parser.add_argument("--second-export", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    first = snapshot(args.first_layout)
    second = snapshot(args.second_layout)
    assert first == second, "production import actions are not reproducible across equivalent tar and gzip inputs"
    # The two export targets intentionally have different tags. Their image
    # configurations and ordered layers must still be identical.
    assert check_export(args.first_export, args.architecture) == check_export(args.second_export, args.architecture)
    print(json.dumps({"architecture": args.architecture, "import_layouts_identical": True, "base_layers_preserved": 2, "consumer_layers_added": 1, "config_and_rootfs_preserved": True}, sort_keys=True))


if __name__ == "__main__":
    main()
