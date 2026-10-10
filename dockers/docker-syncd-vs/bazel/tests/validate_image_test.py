#!/usr/bin/env python3
"""Exercise syncd-vs OCI checks with complete synthetic OCI layouts and tars."""

import copy
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
import source_packages


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
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        self.ssh_elf = bytes(header) + b"FIPS OpenSSH runtime"
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
        packages = [(package, "1.0", [("usr/share/" + package + "/data", package.encode(), 0o644)])]
        if variant == "runtime":
            packages.append(("openssh-client", "1.0+fips", [("usr/bin/ssh", self.ssh_elf, 0o755)]))
        records, entries = [], []
        for name, version, members in packages:
            data = layer(members)
            records.append({"package": name, "version": version, "architecture": "amd64",
                "source_deb": name + "_" + version + "_amd64.deb", "source_size": 1,
                "source_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "control_sha256": hashlib.sha256((name + " control").encode()).hexdigest(),
                "control_fields": {"Package": name, "Version": version, "Architecture": "amd64"},
                "payload_sha256": hashlib.sha256(data).hexdigest(), "payload_size": len(data),
                "payload_members": len(members)})
            entries.extend(members)
        payload.write_bytes(layer(entries))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        value = {"schema": 1, "image": "docker-syncd-vs", "variant": variant,
                 "architecture": "amd64", "distribution": "trixie", "features": dict(validate_payloads.FEATURES),
                 "required_packages": [record["package"] for record in records], "debug_apt_packages": [], "packages": records,
                 "payload": {"path": "payload.tar", "sha256": digest, "size": payload.stat().st_size, "members": len(entries)}}
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

    def validate(self, **kwargs):
        return subject.validate_images(self.root / "runtime.oci", self.root / "debug.oci",
                                       self.runtime_handoff, self.debug_handoff,
                                       self.runtime_manifest, self.debug_manifest, ROOT, fixture=True, **kwargs)

    def source_archives(self):
        """Model owner tar payloads independently of Make's remaining package handoff."""
        records = copy.deepcopy(json.loads((OWNER / "bazel/source_packages.json").read_bytes())["packages"])
        directory = self.root / "source"
        directory.mkdir()
        runtime_entries, debug_entries = [], []
        for index, record in enumerate(records):
            package = record["package"]
            runtime = []
            for name, kind in record.pop("required_paths").items():
                if kind == "symlink":
                    # The OCI fixture helper supports regular files only, so
                    # the source inventory here contains the substantive paths.
                    continue
                data = self.ssh_elf + package.encode() if kind == "elf" else b"source data"
                runtime.append((name, data, 0o755 if kind == "elf" else 0o644))
            debug = [("usr/lib/debug/.build-id/ab/" + str(index) * 38 + ".debug",
                      self.ssh_elf + b"symbols" + package.encode(), 0o644)]
            runtime_path, debug_path = directory / (package + ".tar"), directory / (package + "-debug.tar")
            runtime_path.write_bytes(layer(runtime))
            debug_path.write_bytes(layer(debug))
            record.update(input_tar_sha256=subject.sha(runtime_path), files=source_packages.inventory(runtime_path),
                          debug={"input_tar_sha256": subject.sha(debug_path), "files": source_packages.inventory(debug_path)})
            runtime_entries.extend(runtime)
            debug_entries.extend(debug)
        runtime_tar, debug_tar = directory / "runtime.tar", directory / "debug.tar"
        runtime_tar.write_bytes(layer(runtime_entries))
        debug_tar.write_bytes(layer(debug_entries))
        receipt = {**source_packages.IDENTITY, "kind": "bazel_source", "packages": records,
                   "contract_sha256": subject.sha(OWNER / "bazel/source_packages.json"),
                   "base_manifest_digest": "sha256:" + "b" * 64,
                   "module_file_sha256": {name: "a" * 64 for name in ("sonic-swss-common", "sonic-sairedis")}}
        for field, path in (("payload", runtime_tar), ("debug_payload", debug_tar)):
            receipt[field] = {"sha256": subject.sha(path), "size": path.stat().st_size,
                              "members": len(source_packages.inventory(path))}
        receipt_path = directory / "receipt.json"
        receipt_path.write_text(json.dumps(receipt))
        self.runtime_layers.append(runtime_tar.read_bytes())
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes(), debug_tar.read_bytes()]
        self.write_images()
        return {"source_runtime_tar": runtime_tar, "source_debug_tar": debug_tar, "source_receipt": receipt_path}

    def test_source_payloads_and_receipt_are_bound_to_the_images(self):
        """Final validation proves the reused source bytes and their recorded owners are present."""
        inputs = self.source_archives()
        result = self.validate(**inputs)
        self.assertEqual(result["source_receipt_sha256"], subject.sha(inputs["source_receipt"]))
        self.assertEqual({item["package"] for item in result["source_packages"]["packages"]}, set(source_packages.PACKAGES))
        self.assertGreater(result["source_runtime_payload_entries"], 3)
        self.assertEqual(result["source_debug_payload_entries"], 3)
        inputs["source_runtime_tar"].write_bytes(inputs["source_runtime_tar"].read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "source receipt archive hash differs"):
            self.validate(**inputs)

    def test_debug_cannot_replace_a_source_library(self):
        """Debug adds matching symbols while preserving every source-built runtime ELF."""
        inputs = self.source_archives()
        self.debug_layers.append(layer([("usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0",
                                         self.ssh_elf + b"different library", 0o755)]))
        self.write_images()
        with self.assertRaisesRegex(ValueError, "debug image changes the source runtime payload"):
            self.validate(**inputs)

    def test_missing_source_layer_is_rejected(self):
        """A valid source receipt cannot stand in for installing its runtime library archive."""
        inputs = self.source_archives()
        self.runtime_layers.pop()
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes(), inputs["source_debug_tar"].read_bytes()]
        self.write_images()
        with self.assertRaisesRegex(ValueError, "runtime source package payload differs"):
            self.validate(**inputs)

    def test_source_inputs_must_be_complete_and_are_required_for_production(self):
        """Require both owner archives and their receipt before claiming complete validation."""
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            self.validate(source_runtime_tar=self.runtime_payload)
        complete = {name: self.runtime_payload for name in
                    ("base_path", "apt_layer", "debug_tools_layer", "runtime_apt_selection", "debug_apt_selection",
                     "package_state_contract", "runtime_archive", "debug_archive")}
        with self.assertRaisesRegex(ValueError, "complete validation requires source"):
            subject.validate_images(self.root / "runtime.oci", self.root / "debug.oci", self.runtime_handoff,
                                    self.debug_handoff, self.runtime_manifest, self.debug_manifest, ROOT, **complete)

    def test_complete_fixture_preserves_configuration_and_payloads(self):
        result = self.validate()
        self.assertEqual(result["validation_mode"], "fixture")
        self.assertEqual(result["runtime_layers"], 3)
        self.assertEqual(result["debug_layers"], 4)
        self.assertEqual(result["runtime_package_count"], 2)
        self.assertEqual(result["allowed_debug_package_replacements"], [])
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


    def test_debug_cannot_supply_a_separate_openssh_package(self):
        """Even identical FIPS bytes must come from the runtime handoff, not a debug override."""
        self.append_debug_package("openssh-client", "1.0+fips", [("usr/bin/ssh", self.ssh_elf, 0o755)])
        self.debug_layers = self.runtime_layers + [self.debug_payload.read_bytes()]
        self.write_images()
        with self.assertRaisesRegex(ValueError, "must inherit runtime FIPS openssh-client"):
            self.validate()

    def test_debug_cannot_mutate_the_runtime_openssh_elf(self):
        """Reject an unlisted overlay that changes the runtime FIPS client's deployed bytes."""
        self.debug_layers.append(layer([("usr/bin/ssh", self.ssh_elf + b"changed", 0o755)]))
        self.write_images()
        with self.assertRaisesRegex(ValueError, "debug image changes the runtime package payload"):
            self.validate()

    def test_runtime_cannot_use_the_ordinary_openssh_package(self):
        """An ordinary Debian client cannot satisfy the FIPS runtime handoff contract."""
        manifest = json.loads(self.runtime_handoff.read_bytes())
        manifest["packages"][-1]["version"] = "1.0"
        manifest["packages"][-1]["control_fields"]["Version"] = "1.0"
        self.runtime_handoff.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(ValueError, "requires the Make FIPS openssh-client"):
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
        with self.assertRaisesRegex(ValueError, "changes a base directory link"):
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


    def test_checked_overlay_preserves_links_to_inherited_elfs(self):
        elf = {"kind": "file", "elf_machine": 62, "sha256": "a" * 64}
        for kind, target in (("hardlink", "usr/lib/library.so.1"),
                             ("symlink", "library.so.1"),
                             ("symlink", "/usr/lib/library.so.1")):
            with self.subTest(kind=kind, target=target):
                link = {"kind": kind, "linkname": target}
                base = {"usr/lib/library.so.1": elf, "usr/lib/library.so": link}
                subject.assert_overlay_paths({"usr/lib/library.so": link}, base)
                for replacement in ({**elf, "sha256": "b" * 64},
                                    {"kind": "symlink", "linkname": "other.so"}):
                    with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
                        subject.assert_overlay_paths({"usr/lib/library.so": replacement}, base)

    def test_inherited_elf_links_resolve_image_directory_aliases(self):
        base = {"lib": {"kind": "symlink", "linkname": "usr/lib"},
                "usr/lib/library.so.1": {"kind": "file", "elf_machine": 62},
                "usr/lib/library.so": {"kind": "symlink", "linkname": "/lib/library.so.1"},
                "etc/cycle": {"kind": "symlink", "linkname": "cycle"}}
        with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
            subject.assert_overlay_paths({"usr/lib/library.so": {"kind": "symlink", "linkname": "other.so"}}, base)
        subject.assert_overlay_paths({"etc/cycle": {"kind": "file"}}, base)

    def test_checked_overlay_preserves_directories_and_aliases_containing_elfs(self):
        base = {"lib": {"kind": "symlink", "linkname": "usr/lib"},
                "usr/lib": {"kind": "directory"},
                "usr/lib/library.so.1": {"kind": "file", "elf_machine": 62}}
        subject.assert_overlay_paths({"lib": base["lib"], "usr/lib": base["usr/lib"]}, base)
        with self.assertRaisesRegex(ValueError, "changes a base directory link"):
            subject.assert_overlay_paths({"lib": {"kind": "symlink", "linkname": "opt/lib"}}, base)
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "hides a base ELF directory"):
                    subject.assert_overlay_paths({"usr/lib": {"kind": kind, "linkname": "elsewhere"}}, base)

    def vim_tools(self, *, target="/usr/bin/vim.basic", uid=0, replace_binary=False, whiteout=False):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 62)
        tiny = self.root / "vim-base.tar"
        tiny.write_bytes(layer([("usr/bin/vim", bytes(header) + b"tiny", 0o755)]))
        files = {}
        subject.apply_layer(tiny, files)
        files["usr/bin/vim.tiny"] = {"kind": "hardlink", "mode": 0o755, "uid": 0, "gid": 0,
                                    "linkname": "usr/bin/vim"}
        files.update({name: {"kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0,
                             "linkname": "/usr/bin/vim.tiny"} for name in subject.VIM_ALTERNATIVES})
        path = self.root / "vim-tools.tar"
        with tarfile.open(path, "w") as archive:
            for name in subject.VIM_ALTERNATIVES:
                member = tarfile.TarInfo(name)
                member.type, member.mode, member.uid, member.linkname = tarfile.SYMTYPE, 0o777, uid, target
                archive.addfile(member)
            binaries = [("usr/bin/vim.basic", bytes(header) + b"basic")]
            if replace_binary:
                binaries.append(("usr/bin/vim", bytes(header) + b"replacement"))
            if whiteout:
                binaries.append(("usr/bin/.wh.vim.tiny", b""))
            for name, data in binaries:
                member = tarfile.TarInfo(name)
                member.mode, member.size = 0o755, len(data)
                archive.addfile(member, io.BytesIO(data))
        return path, files

    def test_debug_vim_alternatives_allow_only_the_declared_link_change(self):
        path, files = self.vim_tools()
        old_binary = dict(files["usr/bin/vim.tiny"])
        with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
            subject.apply_layer(path, dict(files), checked_overlay=True)
        subject.apply_debug_tools_layer(path, files)
        self.assertEqual(files["usr/bin/vim.tiny"], old_binary)
        for name in subject.VIM_ALTERNATIVES:
            self.assertEqual(files[name]["linkname"], "/usr/bin/vim.basic")

    def test_debug_vim_alternatives_reject_other_targets_or_owners(self):
        for kwargs in ({"target": "/usr/bin/other"}, {"uid": 1}):
            with self.subTest(kwargs=kwargs):
                path, files = self.vim_tools(**kwargs)
                with self.assertRaisesRegex(ValueError, "unexpected debug Vim alternative"):
                    subject.apply_debug_tools_layer(path, files)
        path, files = self.vim_tools()
        files["etc/alternatives/editor"]["linkname"] = "/usr/bin/other"
        with self.assertRaisesRegex(ValueError, "unexpected debug Vim alternative"):
            subject.apply_debug_tools_layer(path, files)

    def test_debug_vim_policy_preserves_binary_and_whiteout_checks(self):
        for kwargs, message in (({"replace_binary": True}, "change an inherited ELF"),
                                ({"whiteout": True}, "contains a whiteout")):
            with self.subTest(kwargs=kwargs):
                path, files = self.vim_tools(**kwargs)
                before = dict(files)
                with self.assertRaisesRegex(ValueError, message):
                    subject.apply_debug_tools_layer(path, files)
                self.assertEqual(files, before)


if __name__ == "__main__":
    unittest.main()
