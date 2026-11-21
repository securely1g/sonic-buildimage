"""Exercise prepared Docker-store cache hits, invalidation and safe fallback."""

import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "installer_docker_store_cache", ROOT / "scripts/installer_docker_store_cache.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


class InstallerDockerStoreCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "root"
        self.source = self.directory / "source"
        self.store = self.directory / "cache"
        self.state = self.directory / "state.json"
        self.output = self.directory / "dockerfs.tar.gz"
        self.archive = self.directory / "docker-example.gz"
        self.template = self.directory / "rendered.sh"
        self.archive.write_bytes(b"selected container archive")
        self.template.write_text("load; tag; verify; generate shutdown order")
        for name in ("build_debian.sh", "scripts/installer_docker_store_cache.py",
                     "files/docker/docker", "files/build_templates/packages.json.j2",
                     "files/build_templates/default_manifest", "onie-image.conf"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        (self.source / ".arch").write_text("amd64")
        generator = self.root / "usr/local/bin/generate_shutdown_order.py"
        generator.parent.mkdir(parents=True)
        generator.write_text("generate two shutdown orders")
        daemon = self.root / "usr/bin/dockerd"
        daemon.parent.mkdir(parents=True)
        daemon.write_bytes(b"fixture daemon")
        (self.root / "etc/sonic").mkdir(parents=True)
        for name in cache.ORDERS:
            path = self.root / "etc/sonic" / name
            path.write_text("syncd swss database")
            path.chmod(0o644)
        wheel = self.directory / "utilities.whl"
        wheel.write_bytes(b"utility wheel")
        self.env = {
            "TARGET_MACHINE": "vs", "IMAGE_TYPE": "onie", "SONIC_IMAGE_VERSION": "version-1",
            "installer_images": f"swss|dockers/example||{self.archive}:1",
            "sonic_utilities_py3_wheel_path": str(wheel),
            "SONIC_INSTALLER_DOCKER_STORE_RECEIPT": str(self.directory / "receipt.json"),
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(cache, "HERE", self.source).start()
        mock.patch.dict(os.environ, self.env, clear=True).start()
        mock.patch.object(cache.shutil, "which", return_value=str(wheel)).start()
        info = {
            "Images": 0, "Containers": 0, "ServerVersion": "28.5.2", "Driver": "overlay2",
            "DriverStatus": [["Backing Filesystem", "extfs"]], "Architecture": "x86_64",
            "OSType": "linux", "KernelVersion": "fixture",
        }
        mock.patch.object(cache.subprocess, "check_output", side_effect=lambda command, **kwargs:
                          json.dumps(info) if "docker" in command else b"docker=28.5.2\n").start()

        def install(command, **kwargs):
            self.assertEqual(command[:8], ["sudo", "install", "-o", "root", "-g", "root", "-m", "0644"])
            shutil.copyfile(command[-2], command[-1])
            Path(command[-1]).chmod(0o644)
            return subprocess.CompletedProcess(command, 0)

        mock.patch.object(cache.subprocess, "run", side_effect=install).start()

    def prepare(self, mode="read"):
        return cache.prepare(self.root, "unix:///fixture.sock", self.template,
                             self.state, self.output, str(self.store), mode)

    def seed(self):
        self.assertFalse(self.prepare("readwrite"))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            data = b"fixture layer metadata"
            entry = tarfile.TarInfo("image/overlay2/layerdb/sha256/fixture/cache-id")
            entry.size = len(data)
            entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(data))
        self.output.write_bytes(gzip.compress(stream.getvalue(), mtime=0))
        cache.publish(self.root, self.state, self.output, str(self.store), "readwrite")
        return self.store / cache.read_json(self.state)["key"]

    def test_cold_then_warm_restores_exact_archive_and_shutdown_metadata(self):
        self.seed()
        original = self.output.read_bytes()
        self.output.unlink()
        for name in cache.ORDERS:
            (self.root / "etc/sonic" / name).write_text("stale")
        self.assertTrue(self.prepare())
        self.assertEqual(self.output.read_bytes(), original)
        with tarfile.open(self.output, "r:gz") as archive:
            entry = archive.getmembers()[0]
            self.assertEqual(entry.mode, 0o600)
            self.assertEqual(archive.extractfile(entry).read(), b"fixture layer metadata")
        self.assertTrue(cache.hit(self.state))
        for name in cache.ORDERS:
            self.assertEqual((self.root / "etc/sonic" / name).read_text(), "syncd swss database")
        receipt = cache.read_json(self.directory / "receipt.json")
        self.assertEqual(receipt["status"], "hit")
        self.assertTrue(receipt["loading_skipped"])
        self.assertTrue(receipt["packing_skipped"])
        self.output.write_bytes(b"changed after loading was skipped")
        with self.assertRaises(ValueError):
            cache.hit(self.state)

    def test_changed_archive_tags_template_or_tooling_cannot_reuse_old_store(self):
        self.seed()
        original_key = cache.read_json(self.state)["key"]
        mutations = (
            (self.archive, b"different container"),
            (self.template, b"changed tagging behavior"),
            (self.root / "usr/local/bin/generate_shutdown_order.py", b"new metadata generator"),
            (self.root / "usr/bin/dockerd", b"different daemon with the same version"),
        )
        for path, data in mutations:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(data)
                self.assertFalse(self.prepare())
                self.assertNotEqual(cache.read_json(self.state)["key"], original_key)
                path.write_bytes(original)
        with mock.patch.dict(os.environ, {"SONIC_IMAGE_VERSION": "version-2"}):
            self.assertFalse(self.prepare())
            self.assertNotEqual(cache.read_json(self.state)["key"], original_key)

    def test_corruption_and_incomplete_entries_fall_back_without_replacing_output(self):
        entry = self.seed()
        original = self.output.read_bytes()
        for corruption in ("archive", "manifest", "orders", "symlink"):
            with self.subTest(corruption=corruption):
                manifest = (entry / "manifest.json").read_bytes()
                archive = (entry / "dockerfs.tar.gz").read_bytes()
                if corruption == "archive":
                    (entry / "dockerfs.tar.gz").write_bytes(b"truncated")
                elif corruption == "manifest":
                    (entry / "manifest.json").write_text("{")
                elif corruption == "symlink":
                    (entry / "dockerfs.tar.gz").unlink()
                    (entry / "dockerfs.tar.gz").symlink_to(self.output)
                else:
                    value = json.loads(manifest)
                    value["orders"] = {"../unexpected": "unsafe"}
                    cache.write_json(entry / "manifest.json", value)
                self.assertFalse(self.prepare())
                self.assertEqual(cache.read_json(self.state)["status"], "miss")
                self.assertFalse(cache.hit(self.state))
                self.assertEqual(self.output.read_bytes(), original)
                (entry / "dockerfs.tar.gz").unlink()
                (entry / "dockerfs.tar.gz").write_bytes(archive)
                (entry / "manifest.json").write_bytes(manifest)
                self.assertFalse(list(self.directory.glob(".docker-store-restore-*")))

    def test_readonly_and_off_modes_do_not_publish_or_create_cache_directories(self):
        self.assertFalse(self.prepare())
        self.assertFalse(self.store.exists())
        cache.publish(self.root, self.state, self.output, str(self.store), "read")
        self.assertFalse(self.store.exists())
        with mock.patch.object(cache, "inputs") as inputs:
            self.assertFalse(self.prepare("off"))
            inputs.assert_not_called()
        self.assertEqual(cache.read_json(self.state)["status"], "disabled")

    def test_failed_hit_state_commit_falls_back_and_capacity_never_evicts(self):
        entry = self.seed()
        write = cache.write_json

        def reject_hit(path, value):
            if path == self.state and value.get("status") == "hit":
                raise OSError("no space")
            return write(path, value)

        with mock.patch.object(cache, "write_json", side_effect=reject_hit):
            self.assertFalse(self.prepare())
        self.assertFalse(cache.hit(self.state))
        self.archive.write_bytes(b"another container")
        self.assertFalse(self.prepare("readwrite"))
        with mock.patch.object(cache, "CACHE_BUDGET", 0):
            cache.publish(self.root, self.state, self.output, str(self.store), "readwrite")
        self.assertTrue(entry.exists())
        self.assertEqual(len(list(self.store.glob("*/manifest.json"))), 1)

    def test_unknown_or_malformed_state_cannot_select_normal_compression(self):
        for value in ({"version": 2, "status": "hit"}, {"version": 1, "status": "unexpected"},
                      {"version": 1, "status": "hit"}):
            with self.subTest(value=value):
                cache.write_json(self.state, value)
                with self.assertRaises((ValueError, KeyError)):
                    cache.hit(self.state)

    def test_dynamic_or_unsupported_profiles_are_disabled(self):
        for settings in ({"TARGET_MACHINE": "other"}, {"IMAGE_TYPE": "kvm"},
                         {"CROSS_BUILD_ENVIRON": "y"}, {"BUILD_REDUCE_IMAGE_SIZE": "y"},
                         {"sonic_packages": "dynamic|repository|latest"},
                         {"include_kubernetes": "y"}, {"TAR_OPTIONS": "--mtime=now"}):
            with self.subTest(settings=settings), mock.patch.dict(os.environ, settings):
                self.assertFalse(self.prepare())
                self.assertEqual(cache.read_json(self.state)["status"], "disabled")
        hook = self.source / "files/build_templates/build_debian.organization.sh"
        hook.write_text("custom hook")
        self.assertFalse(self.prepare())
        self.assertEqual(cache.read_json(self.state)["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
