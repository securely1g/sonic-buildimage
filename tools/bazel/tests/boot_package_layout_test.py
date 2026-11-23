"""Exercise the offline package manager's layout source without a Docker daemon."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import jinja2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.bazel.image import merge_layers
from tools.bazel.oci import oci_layout
from tools.bazel.tests.oci_base_fixture import digest, image_fixture, oci_files, write_layout


class FakePackageSource:
    def __init__(self, database, docker, metadata_resolver):
        self.metadata_resolver = metadata_resolver

    def get_package(self):
        return SimpleNamespace(name=self.get_metadata()["name"], repository=None, entry=SimpleNamespace())


class FakeMetadataResolver:
    def __init__(self, docker, registry):
        pass

    def from_labels(self, labels):
        return labels


class BootPackageLayoutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config, layers = image_fixture()
        self.tag = "docker-dhcp-relay:latest"
        config = json.loads(config)
        config["config"]["Labels"] = {"name": "dhcp-relay"}
        config = json.dumps(config).encode()
        self.image_id = digest(config)
        self.layout = self.root / "layout"
        write_layout(self.layout, oci_files(config, layers))
        modules = {
            "merge_layers": merge_layers, "oci_layout": oci_layout,
            "sonic_package_manager": SimpleNamespace(PackageManager=object),
            "sonic_package_manager.metadata": SimpleNamespace(MetadataResolver=FakeMetadataResolver),
            "sonic_package_manager.source": SimpleNamespace(PackageSource=FakePackageSource),
        }
        imports = patch.dict(sys.modules, modules)
        imports.start()
        self.addCleanup(imports.stop)
        spec = importlib.util.spec_from_file_location("boot_packages", ROOT / "scripts/build_boot_packages.py")
        self.subject = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.subject)
        self.manager = MagicMock()
        self.manager.metadata_resolver.from_labels.side_effect = lambda labels: labels
        self.manager.is_installed.return_value = False

    def second_layout(self):
        config, layers = image_fixture()
        config = json.loads(config)
        config["config"]["Labels"] = {"name": "other"}
        config = json.dumps(config).encode()
        image_id = digest(config)
        layout = self.root / "layout with spaces"
        write_layout(layout, oci_files(config, layers))
        for blob in (layout / "blobs/sha256").iterdir():
            shared = self.layout / "blobs/sha256" / blob.name
            if shared.exists():
                blob.unlink()
                os.link(shared, blob)
        return layout, "other:latest", image_id

    def run_helper(self, commands, batch=True, plan_override=None):
        labels = self.root / "labels.json"
        inventory = self.root / "inventory.json"
        tags, metadata = {}, {}
        for command in commands:
            layout, tag, _ = command[1:4]
            directory = Path(layout)
            index = json.loads((directory / "index.json").read_text())
            manifest = json.loads((directory / "blobs/sha256" / index["manifests"][0]["digest"][7:]).read_text())
            image_id = manifest["config"]["digest"]
            config = json.loads((directory / "blobs/sha256" / image_id[7:]).read_text())
            tags[tag] = image_id
            metadata[tag] = config["config"]["Labels"]
        labels.write_text(json.dumps(metadata))
        inventory.write_text(json.dumps(tags))
        factory = MagicMock(return_value=self.manager)
        with patch.object(self.subject, "PackageManager", SimpleNamespace(get_manager=factory)):
            if batch:
                plan = self.root / "plan.jsonl"
                plan.write_text(plan_override if plan_override is not None else
                                "".join(json.dumps(command) + "\n" for command in commands))
                with patch.object(sys, "argv", ["helper", str(labels), str(inventory), "install-batch", str(plan)]):
                    self.subject.main()
            else:
                for command in commands:
                    with patch.object(sys, "argv", ["helper", str(labels), str(inventory), "install", *command]):
                        self.subject.main()
        return factory

    def test_package_registration_and_cli_files_come_from_selected_layout(self):
        destination = self.root / "host/config/plugins/dhcp_relay.py"
        def install(source, **kwargs):
            self.assertEqual(kwargs, {"enable": True, "default_owner": "local"})
            package = source.get_package()
            source.install(package)
            self.assertEqual(package.name, "dhcp-relay")
            self.assertEqual(package.entry.image_id, self.image_id)
            self.assertEqual(package.entry.docker_image_reference, self.tag)
            self.assertEqual(package.entry.repository, "docker-dhcp-relay")
            with patch("os.chown") as chown:
                self.manager.docker.extract(self.image_id, "/etc/base", destination)
            chown.assert_called_once_with(destination, 0, 0)
            self.assertEqual(destination.read_bytes(), b"updated by second layer\n")
            with self.assertRaisesRegex(ValueError, "Missing regular CLI plugin"):
                self.manager.docker.extract(self.image_id, "/etc/deleted", destination)
        self.manager.is_installed.return_value = False
        self.manager.install_from_source.side_effect = install
        original = self.manager.docker.extract
        self.subject.install_layout(self.manager, self.layout, self.tag, "linux/amd64",
                                    {self.tag: self.image_id}, {"enable": True, "default_owner": "local"})
        self.manager.install_from_source.assert_called_once()
        self.assertIs(self.manager.docker.extract, original)
        self.manager.docker.load.assert_not_called()

    def test_mismatched_bundle_fails_before_package_registration(self):
        with self.assertRaisesRegex(ValueError, "differs from the installed bundle"):
            self.subject.install_layout(self.manager, self.layout, self.tag, "linux/amd64",
                                        {self.tag: "sha256:" + "0" * 64}, {})
        self.manager.install_from_source.assert_not_called()
        self.manager.upgrade_from_source.assert_not_called()

    def test_batch_preserves_order_options_and_hashes_shared_files_once(self):
        layout, tag, _ = self.second_layout()
        commands = [
            ["--from-layout", str(self.layout), self.tag, "linux/amd64", "--enable", "--set-owner", "local"],
            ["--from-layout", str(layout), tag, "linux/amd64", "--set-owner", "owner with spaces"],
        ]
        records = []
        original = self.manager.docker.extract
        self.manager.install_from_source.side_effect = (
            lambda source, **options: records.append((source.get_package().name, options)))
        real_hash = self.subject.hashlib.file_digest
        for batch in (False, True):
            with self.subTest(batch=batch):
                records.clear()
                # Count only helper verification, not fixture inventory generation.
                with patch.object(self.subject.hashlib, "file_digest", wraps=real_hash) as hashes:
                    factory = self.run_helper(commands, batch)
                expected = [
                    ("dhcp-relay", {"enable": True, "default_owner": "local"}),
                    ("other", {"enable": False, "default_owner": "owner with spaces"}),
                ]
                self.assertEqual(records, expected)
                self.assertEqual(factory.call_count, 1 if batch else 2)
                self.assertIs(self.manager.docker.extract, original)
                count = hashes.call_count
                if not batch:
                    serial_hashes = count
                else:
                    self.assertLess(count, serial_hashes)
        self.manager.docker.load.assert_not_called()

    def test_batch_stops_on_failure_and_restores_the_extraction_hook(self):
        layout, tag, _ = self.second_layout()
        commands = [
            ["--from-layout", str(self.layout), self.tag, "linux/amd64"],
            ["--from-layout", str(layout), tag, "linux/amd64"],
            ["--from-layout", str(self.layout), self.tag, "linux/amd64"],
        ]
        original = self.manager.docker.extract
        self.manager.install_from_source.side_effect = [None, RuntimeError("registration failed")]
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            self.run_helper(commands)
        self.assertEqual(self.manager.install_from_source.call_count, 2)
        self.assertIs(self.manager.docker.extract, original)

    def test_batch_rehashes_modified_or_replaced_shared_layers(self):
        layout, tag, _ = self.second_layout()
        commands = [
            ["--from-layout", str(self.layout), self.tag, "linux/amd64"],
            ["--from-layout", str(layout), tag, "linux/amd64"],
        ]
        layer = oci_layout.validate_layout(self.layout, "linux/amd64").layers[0]
        data, info = layer.read_bytes(), layer.stat()
        for replace in (False, True):
            with self.subTest(replace=replace):
                def change(source, **options):
                    if replace:
                        pending = layer.with_name("replacement")
                        pending.write_bytes(b"x" * len(data))
                        pending.replace(layer)
                        second = layout / "blobs/sha256" / layer.name
                        second.unlink()
                        os.link(layer, second)
                    else:
                        layer.write_bytes(b"x" * len(data))
                    os.utime(layer, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.manager.install_from_source.reset_mock()
                self.manager.install_from_source.side_effect = change
                with self.assertRaisesRegex(ValueError, "digest or size mismatch"):
                    self.run_helper(commands)
                self.assertEqual(self.manager.install_from_source.call_count, 1)
                layer.write_bytes(data)

    def test_change_during_hashing_is_rejected(self):
        blob = self.root / "changing"
        blob.write_bytes(b"original")
        real_hash = self.subject.hashlib.file_digest
        def change(stream, algorithm):
            result = real_hash(stream, algorithm)
            blob.write_bytes(b"modified")
            return result
        with patch.object(self.subject.hashlib, "file_digest", side_effect=change):
            with self.assertRaisesRegex(ValueError, "changed while verifying"):
                self.subject.VerifiedFiles().digest(blob)

    def test_invalid_batch_records_and_nonlayout_sources_do_not_register(self):
        command = ["--from-layout", str(self.layout), self.tag, "linux/amd64"]
        for record in ({}, [], ["--from-layout", None], ["--from-tarball", "archive"]):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    self.run_helper([command], plan_override=json.dumps(record) + "\n")
                self.manager.install_from_source.assert_not_called()
                self.manager.install.assert_not_called()

    def test_template_batches_native_layouts_and_flushes_before_legacy_calls(self):
        import subprocess

        source = (ROOT / "files/build_templates/sonic_debian_extension.j2").read_text()
        gate = source[source.index("batch_registrations=n\n"):source.index("\n. functions.sh")]
        start = source.index("{% macro get_install_options")
        end = source.index("if [[ ${DOCKER_LOAD_ON_BOOT:-n} == y ]]; then\n    # Preserve any extra tags", start)
        template = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(source[start:end])
        packages, paths = [], {}
        for name, directory in (("first", True), ("legacy", False), ("last", True)):
            path = self.root / (name + " input")
            if directory:
                path.mkdir()
            else:
                path.write_bytes(b"legacy")
            marker = "__PATH_" + name + "__"
            paths[marker] = str(path)
            packages.append(f"{name}.gz|{marker}|local|y")
        commands = self.root / "commands"
        commands.mkdir()
        helper = commands / "helper"
        helper.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ["TRACE"],"a") as out:
    out.write(json.dumps({"process":args[0]})+"\\n")
    if args[0]=="install-batch":
        plan=pathlib.Path(os.environ["FILESYSTEM_ROOT"])/args[1].lstrip("/")
        operations=[json.loads(line) for line in plan.read_text().splitlines()]
    else:
        operations=[args[1:]]
    for operation in operations:
        out.write(json.dumps({"install":operation})+"\\n")
''')
        helper.chmod(0o755)
        docker = commands / "docker"
        docker.write_text('#!/bin/sh\n[ "$1" != images ] || printf "latest\\n"\n')
        docker.chmod(0o755)
        root = self.root / "root"
        (root / "tmp/helper").mkdir(parents=True)
        work = self.root / "work"
        work.mkdir()
        prefix = '''set -e
sudo() {
    while [[ "$1" == *=* ]]; do shift; done
    if [[ "$1" == chroot ]]; then shift 2; "$@"; else "$@"; fi
}
package_install=(helper install)
boot_package_manager=(helper)
''' + gate
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                   FILESYSTEM_ROOT=str(root), boot_work=str(work), boot_helper="/tmp/helper",
                   CONFIGURED_PLATFORM="vs", CONFIGURED_ARCH="amd64", DOCKER_LOAD_ON_BOOT="y",
                   BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", CROSS_BUILD_ENVIRON="n",
                   MULTIARCH_QEMU_ENVIRON="n", SONIC_VS_LAYOUT_INPUTS="", SONIC_IMAGE_VERSION="test")
        for selected in (packages, [packages[0], packages[2]]):
            script = prefix + template.render(sonic_packages="", sonic_local_packages=" ".join(selected))
            # Replace list placeholders after rendering to exercise shell quoting.
            for marker, path in paths.items():
                script = script.replace(marker, path)
            baseline = None
            for flag, extra in (("0", {}), ("1", {}), ("1", {"CROSS_BUILD_ENVIRON": "y"}),
                                ("1", {"CONFIGURED_ARCH": "arm64"}), ("1", {"CONFIGURED_PLATFORM": "other"}),
                                ("1", {"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"})):
                trace = self.root / "trace"
                trace.write_text("")
                result = subprocess.run(["bash", "-c", script], env={**env, **extra,
                    "SONIC_VS_BATCH_REGISTRATIONS": flag, "TRACE": str(trace)},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for line in trace.read_text().splitlines()]
                installs = [event["install"] for event in events if "install" in event]
                if baseline is None:
                    baseline = installs
                if not extra.get("CONFIGURED_ARCH"):
                    self.assertEqual(installs, baseline, (flag, extra, events, result.stdout, result.stderr))
                processes = [event["process"] for event in events if "process" in event]
                expected = ["install-batch", "install", "install-batch"] if len(selected) == 3 else ["install-batch"]
                self.assertEqual(processes, expected if flag == "1" and not extra else ["install"] * len(selected))


if __name__ == "__main__":
    unittest.main()
