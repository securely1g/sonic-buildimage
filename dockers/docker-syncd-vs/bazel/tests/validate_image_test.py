#!/usr/bin/env python3
"""Exercise syncd-vs OCI checks with complete synthetic OCI layouts and tars."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

OWNER = Path(__file__).absolute().parents[2]
ROOT = OWNER.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries as layer, write_layout
import validate_image as subject
import validate_payloads


def write_image(path, layers, manifest, *, entrypoint=None):
    config = {
        "architecture": "amd64", "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [digest(data) for data in layers]},
        "config": {"Entrypoint": entrypoint or ["/usr/local/bin/supervisord"],
                   "Env": ["DEBIAN_FRONTEND=noninteractive"],
                   "Labels": {"com.azure.sonic.manifest": json.dumps(manifest, separators=(",", ":"))}},
    }
    write_layout(path, oci_files(json.dumps(config, separators=(",", ":")).encode(), layers))


class ValidateImageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-image-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime_manifest = self.root / "runtime-manifest.json"
        self.debug_manifest = self.root / "debug-manifest.json"
        self.runtime_manifest.write_text('{"container_name":"syncd","version":"1.0.0"}\n')
        self.debug_manifest.write_text('{"container_name":"syncd","version":"1.0.0-dbg"}\n')
        self.runtime_handoff, self.runtime_payload = self.handoff("runtime")
        self.debug_handoff, self.debug_payload = self.handoff("debug", runtime=self.runtime_handoff)
        self.base = layer([("usr/bin/base", b"base\n", 0o755)])
        copied = []
        for source, destination, mode in (
            ("start.sh", "usr/bin/start.sh", 0o755),
            ("supervisord.conf", "etc/supervisor/conf.d/supervisord.conf", 0o644),
            ("critical_processes", "etc/supervisor/critical_processes", 0o644),
        ):
            copied.append((destination, (ROOT / "dockers/docker-syncd-vs/legacy" / source).read_bytes(), mode))
        self.config_layer = layer(copied)
        self.runtime_layers = [self.base, self.runtime_payload.read_bytes(), self.config_layer]
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes()]
        self.write_images()

    def handoff(self, variant, runtime=None):
        directory = self.root / (variant + "-handoff")
        directory.mkdir(parents=True)
        package = "syncd-vs" if variant == "runtime" else "syncd-vs-dbgsym"
        payload = directory / "payload.tar"
        payload.write_bytes(layer([("usr/share/" + package + "/data", package.encode(), 0o644)]))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        record = {"package": package, "version": "1.0", "architecture": "amd64",
                  "source_deb": package + "_1.0_amd64.deb", "source_size": 1,
                  "source_sha256": hashlib.sha256(package.encode()).hexdigest(),
                  "control_sha256": hashlib.sha256((package + " control").encode()).hexdigest(),
                  "payload_sha256": digest, "payload_size": payload.stat().st_size, "payload_members": 1}
        value = {"schema": 1, "image": "docker-syncd-vs", "variant": variant,
                 "architecture": "amd64", "distribution": "trixie", "features": dict(validate_payloads.FEATURES),
                 "required_packages": [package], "debug_apt_packages": [], "packages": [record],
                 "payload": {"path": "payload.tar", "sha256": digest, "size": payload.stat().st_size, "members": 1}}
        if runtime:
            value["runtime_manifest_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
            value["debug_apt_packages"] = ["gdb", "gdbserver", "sshpass", "strace", "vim"]
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        return manifest, payload

    def append_debug_package(self, package, version, entries):
        additional = layer(entries)
        combined = io.BytesIO()
        with tarfile.open(fileobj=combined, mode="w", format=tarfile.GNU_FORMAT) as output:
            for data in (self.debug_payload.read_bytes(), additional):
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                    for member in archive:
                        output.addfile(member, archive.extractfile(member) if member.isfile() else None)
        self.debug_payload.write_bytes(combined.getvalue())
        manifest = json.loads(self.debug_handoff.read_bytes())
        manifest["packages"].append({"package": package, "version": version, "architecture": "amd64",
            "source_deb": package + "_" + version + "_amd64.deb", "source_size": 1,
            "source_sha256": hashlib.sha256((package + version).encode()).hexdigest(),
            "control_sha256": hashlib.sha256((package + " control").encode()).hexdigest(),
            "payload_sha256": hashlib.sha256(additional).hexdigest(), "payload_size": len(additional),
            "payload_members": len(entries)})
        manifest["required_packages"].append(package)
        manifest["payload"] = {"path": "payload.tar", "sha256": hashlib.sha256(combined.getvalue()).hexdigest(),
                               "size": len(combined.getvalue()), "members": sum(item["payload_members"] for item in manifest["packages"])}
        self.debug_handoff.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def write_archive(self, path, *, tag="docker-syncd-vs:latest", config_bytes=None, corrupt_layer=False):
        _, manifest, _, _ = subject.image(self.root / "runtime.oci")
        digest = manifest["config"]["digest"]
        if config_bytes is None:
            config_bytes = (self.root / "runtime.oci/blobs/sha256" / digest[7:]).read_bytes()
        entries = [("config.json", config_bytes, 0o644)]
        entries += [("layer" + str(index) + ".tar", b"changed" if corrupt_layer and index == 0 else data, 0o644)
                    for index, data in enumerate(self.runtime_layers)]
        docker_manifest = [{"Config": "config.json", "RepoTags": [tag],
                            "Layers": ["layer" + str(index) + ".tar" for index in range(len(self.runtime_layers))]}]
        entries.append(("manifest.json", json.dumps(docker_manifest).encode(), 0o644))
        path.write_bytes(gzip.compress(layer(entries), mtime=0))
        return digest

    def write_images(self, *, runtime_entrypoint=None):
        write_image(self.root / "runtime.oci", self.runtime_layers, json.loads(self.runtime_manifest.read_bytes()),
                    entrypoint=runtime_entrypoint)
        write_image(self.root / "debug.oci", self.debug_layers, json.loads(self.debug_manifest.read_bytes()),
                    entrypoint=runtime_entrypoint)

    def validate(self):
        return subject.validate_images(self.root / "runtime.oci", self.root / "debug.oci",
                                       self.runtime_handoff, self.debug_handoff,
                                       self.runtime_manifest, self.debug_manifest, ROOT, fixture=True)

    def test_complete_fixture_preserves_configuration_and_payloads(self):
        result = self.validate()
        self.assertEqual(result["validation_mode"], "fixture")
        self.assertEqual(result["runtime_layers"], 3)
        self.assertEqual(result["debug_layers"], 4)
        self.assertEqual(result["runtime_package_count"], 1)
        self.assertEqual(result["changed_non_elf_runtime_paths_in_debug"], [])

    def test_entrypoint_and_runtime_ancestry_are_required(self):
        self.write_images(runtime_entrypoint=["/bin/false"])
        with self.assertRaisesRegex(ValueError, "entrypoint differs"):
            self.validate()
        self.debug_layers = [layer([("usr/bin/base", b"other base\n", 0o755)])] + self.debug_layers[1:]
        self.write_images()
        with self.assertRaisesRegex(ValueError, "exact runtime OCI layers"):
            self.validate()

    def test_missing_package_payload_is_rejected(self):
        self.runtime_layers = [self.base, self.config_layer]
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes()]
        self.write_images()
        with self.assertRaisesRegex(ValueError, "runtime package payload differs"):
            self.validate()

    def test_debug_cannot_replace_a_deployed_elf(self):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        self.runtime_layers.append(layer([("usr/lib/libexample.so", bytes(header) + b"runtime", 0o644)]))
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes(),
            layer([("usr/lib/libexample.so", bytes(header) + b"changed", 0o644)])]
        self.write_images()
        with self.assertRaisesRegex(ValueError, "changes a deployed ELF"):
            self.validate()


    def test_only_declared_fips_openssh_can_replace_a_runtime_elf(self):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        self.runtime_layers.append(layer([("usr/bin/ssh", bytes(header) + b"runtime", 0o755)]))
        self.append_debug_package("openssh-client", "1.0+fips", [("usr/bin/ssh", bytes(header) + b"FIPS", 0o755)])
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes()]
        self.write_images()
        result = self.validate()
        self.assertEqual(len(result["allowed_debug_package_replacements"]), 1)
        self.assertEqual(result["allowed_debug_package_replacements"][0]["package"], "openssh-client")
        manifest = json.loads(self.debug_handoff.read_bytes())
        manifest["packages"][-1]["version"] = "1.0"
        self.debug_handoff.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(ValueError, "changes a deployed ELF"):
            self.validate()

    def test_docker_archive_tag_config_and_gzip_header_are_checked(self):
        path = self.root / "docker-syncd-vs.gz"
        digest = self.write_archive(path)
        descriptors = subject.image(self.root / "runtime.oci")[1]["layers"]
        result = subject.docker_archive(path, "docker-syncd-vs:latest", digest, descriptors)
        self.assertEqual(result["tag"], "docker-syncd-vs:latest")
        with self.assertRaisesRegex(ValueError, "tag differs"):
            subject.docker_archive(path, "wrong:latest", digest, descriptors)
        raw = bytearray(path.read_bytes())
        raw[4:8] = (1).to_bytes(4, "little")
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "gzip header"):
            subject.docker_archive(path, "docker-syncd-vs:latest", digest, descriptors)
        self.write_archive(path, config_bytes=b"{}")
        with self.assertRaisesRegex(ValueError, "config differs"):
            subject.docker_archive(path, "docker-syncd-vs:latest", digest, descriptors)
        self.write_archive(path, corrupt_layer=True)
        with self.assertRaisesRegex(ValueError, "layer differs"):
            subject.docker_archive(path, "docker-syncd-vs:latest", digest, descriptors)

    def test_complete_mode_requires_all_overlay_inputs(self):
        with self.assertRaisesRegex(ValueError, "complete validation requires"):
            subject.validate_images(self.root / "runtime.oci", self.root / "debug.oci", self.runtime_handoff,
                                    self.debug_handoff, self.runtime_manifest, self.debug_manifest, ROOT)

    def test_opaque_whiteout_removes_only_lower_layer_entries(self):
        first = self.root / "whiteout-base.tar"
        first.write_bytes(layer([("dir/old", b"old", 0o644)]))
        second = self.root / "whiteout-update.tar"
        second.write_bytes(layer([("dir/new", b"new", 0o644), ("dir/.wh..wh..opq", b"", 0o000)]))
        files = {}
        subject.apply_layer(first, files)
        subject.apply_layer(second, files)
        self.assertNotIn("dir/old", files)
        self.assertIn("dir/new", files)

    def test_checked_overlay_rejects_parent_symlink_changes(self):
        base = {"lib": {"kind": "symlink", "linkname": "usr/lib"},
                "usr": {"kind": "directory"}, "usr/lib": {"kind": "directory"}}
        with self.assertRaisesRegex(ValueError, "replaces a directory symlink"):
            subject.assert_overlay_paths({"lib": {"kind": "directory"}}, base)
        with self.assertRaisesRegex(ValueError, "crosses a non-directory"):
            subject.assert_overlay_paths({"lib/library.so": {"kind": "file"}}, base)
        subject.assert_overlay_paths({"usr/lib/library.so": {"kind": "file"}}, base)

    def test_dpkg_filter_uses_the_repository_space_separated_rules(self):
        item = {"kind": "file"}
        files = {name: item for name in ("usr/share/doc/example/README", "usr/share/doc/example/copyright",
                                         "usr/share/man/man1/example.1", "usr/lib/libexample.so")}
        self.assertEqual(set(subject.dpkg_filtered(files, ROOT)),
                         {"usr/share/doc/example/copyright", "usr/lib/libexample.so"})


if __name__ == "__main__":
    unittest.main()
