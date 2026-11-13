"""Bounded read-only metadata fixtures. No DEB fixture is constructed or built."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from tools.bazel.ci import observe_vs_host_binaries as subject


def tar_bytes(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data, options in entries:
            item = tarfile.TarInfo(name)
            for key, value in options.items():
                setattr(item, key, value)
            item.size = len(data) if item.isfile() else 0
            archive.addfile(item, io.BytesIO(data) if item.isfile() else None)
    return result.getvalue()


def wheel_bytes(timestamp=(2000, 1, 1, 0, 0, 0), bad_record=False, extra=None):
    values = {"example/__init__.py": b"answer = 1\n",
              "example-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: example\nVersion: 1.0\nRequires-Dist: dependency>=1\n\n",
              "example-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"}
    values.update(extra or {})
    record = io.StringIO(); writer = csv.writer(record, lineterminator="\n")
    for name, raw in sorted(values.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + ("bad" if bad_record else digest), len(raw)))
    writer.writerow(("example-1.0.dist-info/RECORD", "", ""))
    values["example-1.0.dist-info/RECORD"] = record.getvalue().encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, raw in values.items():
            info = zipfile.ZipInfo(name, timestamp); info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    result.seek(0)
    return result


class HostBinaryObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def original_object(self, data=b"existing bytes"):
        import gzip
        raw = gzip.compress(tar_bytes([("target/input.bin", data, {})]), mtime=0)
        path = self.root / "original.tgz"; path.write_bytes(raw)
        row = {"cache_name": path.name, "cache_bytes": len(raw), "cache_sha256": hashlib.sha256(raw).hexdigest(),
               "member_path": "target/input.bin", "member_bytes": len(data),
               "member_sha256": hashlib.sha256(data).hexdigest(), "required_name": "input.bin"}
        return path, row

    def callback(self, path, row, scratch):
        self.assertEqual(path.read_bytes(), b"existing bytes")
        return {"name": row["required_name"], "observed": True}

    def test_selected_original_member_reaches_callback_without_restore(self):
        path, row = self.original_object()
        self.assertEqual(subject.inspect_cached(path, [row], self.root, self.callback),
                         [{"name": "input.bin", "observed": True}])
        self.assertEqual([x.name for x in self.root.iterdir()], ["original.tgz"])

    def test_outer_hash_mismatch_precedes_callback(self):
        path, row = self.original_object(); row["cache_sha256"] = "a" * 64
        callback = mock.Mock()
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_cached(path, [row], self.root, callback)
        callback.assert_not_called()

    def test_inner_hash_mismatch_precedes_callback(self):
        path, row = self.original_object(); row["member_sha256"] = "a" * 64
        callback = mock.Mock()
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_cached(path, [row], self.root, callback)
        callback.assert_not_called()

    def test_symlink_outer_is_rejected(self):
        path, row = self.original_object(); link = self.root / "link"; link.symlink_to(path)
        with self.assertRaises(OSError):
            subject.inspect_cached(link, [row], self.root, self.callback)

    def test_legacy_member_limit_reuses_the_same_checked_reader(self):
        path, row = self.original_object()
        self.assertTrue(subject.inspect_cached(path, [row], self.root, self.callback, 256 * subject.MIB)[0]["observed"])
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_cached(path, [row], self.root, self.callback, 1024 * subject.MIB)

    def test_callback_cannot_rename_the_selected_original(self):
        path, row = self.original_object()
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_cached(path, [row], self.root, lambda *_: {"name": "other"})

    def test_callback_cannot_mutate_original_temporary_bytes(self):
        path, row = self.original_object()
        def mutate(p, r, _):
            p.write_bytes(b"replaced")
            return {"name": r["required_name"]}
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_cached(path, [row], self.root, mutate)

    def test_tar_timestamp_only_difference_is_separate(self):
        a = subject.tar_metadata(io.BytesIO(tar_bytes([("usr/bin/tool", b"original", {"mtime": 1})])), self.root)[0]
        b = subject.tar_metadata(io.BytesIO(tar_bytes([("usr/bin/tool", b"original", {"mtime": 2})])), self.root)[0]
        self.assertEqual(a["content_metadata_sha256"], b["content_metadata_sha256"])
        self.assertNotEqual(a["timestamps_sha256"], b["timestamps_sha256"])

    def test_modes_and_ownership_are_not_timestamp_equivalence(self):
        a = subject.tar_metadata(io.BytesIO(tar_bytes([("tool", b"original", {"mode": 0o755, "uid": 0})])), self.root)[0]
        b = subject.tar_metadata(io.BytesIO(tar_bytes([("tool", b"original", {"mode": 0o644, "uid": 2})])), self.root)[0]
        self.assertNotEqual(a["content_metadata_sha256"], b["content_metadata_sha256"])

    def test_payload_or_script_bytes_remain_distinct(self):
        a = subject.tar_metadata(io.BytesIO(tar_bytes([("postinst", b"exit 0", {})])), self.root, True)[0]
        b = subject.tar_metadata(io.BytesIO(tar_bytes([("postinst", b"exit 1", {})])), self.root, True)[0]
        self.assertNotEqual(a["content_metadata_sha256"], b["content_metadata_sha256"])

    def test_links_are_observed_without_filesystem_materialization(self):
        raw = tar_bytes([("usr/bin/tool", b"", {"type": tarfile.SYMTYPE, "linkname": "../lib/tool"})])
        value = subject.tar_metadata(io.BytesIO(raw), self.root)[0]
        self.assertEqual(value["entries"][0]["link"], "../lib/tool")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_upstream_deb_identity_mismatch_is_reported(self):
        control = tar_bytes([("control", b"Package: different\nVersion: 1\nArchitecture: amd64\nDepends: libc6 (>= 2.36)\n", {})])
        payload = tar_bytes([("usr/bin/example", b"data", {})])
        with mock.patch.object(subject, "run_upstream", side_effect=[io.BytesIO(control), io.BytesIO(payload)]) as reader:
            value = subject.inspect_deb(io.BytesIO(), "example_1_amd64.deb", self.root)
        self.assertFalse(value["configured_identity_matches"])
        self.assertEqual(reader.call_args_list[0].args[1], ["--ctrl-tarfile"])
        self.assertEqual(reader.call_args_list[1].args[1], ["--fsys-tarfile"])
        self.assertEqual(value["control"]["Depends"], "libc6 (>= 2.36)")

    def test_package_scripts_are_hash_only(self):
        control = tar_bytes([("control", b"Package: example\nVersion: 1\nArchitecture: all\n", {}),
                             ("postinst", b"exit 99\n", {"mode": 0o755})])
        with mock.patch.object(subject, "run_upstream", side_effect=[io.BytesIO(control), io.BytesIO(tar_bytes([]))]):
            value = subject.inspect_deb(io.BytesIO(), "example_1_all.deb", self.root)
        self.assertTrue(value["configured_identity_matches"])
        self.assertFalse(value["package_scripts_executed"])
        self.assertEqual(value["control_files"]["entries"][1]["sha256"], hashlib.sha256(b"exit 99\n").hexdigest())

    def test_wheel_record_and_dependency_metadata(self):
        value = subject.inspect_wheel(wheel_bytes(), "example-1.0-py3-none-any.whl", self.root)
        self.assertTrue(value["configured_identity_matches"])
        self.assertEqual(value["metadata"]["Requires-Dist"], ["dependency>=1"])
        self.assertFalse(value["installed"])

    def test_corrupt_wheel_record_rejected(self):
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_wheel(wheel_bytes(bad_record=True), "example-1.0-py3-none-any.whl", self.root)

    def test_multiple_wheel_metadata_directories_rejected(self):
        with self.assertRaises(subject.original.RecoveryError):
            subject.inspect_wheel(wheel_bytes(extra={"other-1.0.dist-info/METADATA": b"Name: other\nVersion: 1\n"}),
                                  "example-1.0-py3-none-any.whl", self.root)

    def test_wheel_timestamp_only_difference_is_separate(self):
        a = subject.inspect_wheel(wheel_bytes(), "example-1.0-py3-none-any.whl", self.root)
        b = subject.inspect_wheel(wheel_bytes((2001, 1, 1, 0, 0, 0)), "example-1.0-py3-none-any.whl", self.root)
        self.assertEqual(a["semantic_identity"], b["semantic_identity"])
        self.assertNotEqual(a["payload"]["timestamps_sha256"], b["payload"]["timestamps_sha256"])

    def test_duplicate_control_identity_rejected(self):
        with self.assertRaises(subject.original.RecoveryError):
            subject.field_document(b"Package: one\nPackage: two\n")

    def test_elf_abi_facts_are_collected_without_execution(self):
        raw = b"  Class: ELF64\n  Data: 2's complement, little endian\n  Type: DYN\n  Machine: Advanced Micro Devices X86-64\n  OS/ABI: UNIX - System V\n [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]\n 0x1 (NEEDED) Shared library: [libc.so.6]\nVersion needs section '.gnu.version_r':\n Name: GLIBC_2.36 Flags: none\n"
        with mock.patch.object(subject, "run_upstream", return_value=io.BytesIO(raw)):
            value = subject.elf_metadata(io.BytesIO(), self.root)
        self.assertEqual(value["needed"], ["libc.so.6"])
        self.assertEqual(value["required_symbol_versions"], ["GLIBC_2.36"])
        self.assertEqual(value["interpreter"], ["/lib64/ld-linux-x86-64.so.2"])

    def test_timeout_closes_unreaped_group_before_wait(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = -9
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or -9
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")), mock.patch.object(subject.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(subprocess.TimeoutExpired):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        self.assertEqual(order, ["kill", "wait"])

    def test_waitid_success_closes_descendants_before_reaping(self):
        process = mock.Mock(pid=7654321)
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or 0
        state = mock.Mock(si_status=0, si_code=os.CLD_EXITED)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=lambda *_: order.append("observe") or state) as observe, mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")):
            output = subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
            output.close()
        self.assertEqual(order, ["observe", "kill", "wait"])
        self.assertEqual(observe.call_args.args, (os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))

    def test_nonzero_exit_also_closes_descendants_before_reaping(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = 4
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or 4
        state = mock.Mock(si_status=4, si_code=os.CLD_EXITED)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", return_value=state), mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")):
            with self.assertRaises(subject.original.RecoveryError):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        self.assertEqual(order, ["kill", "wait"])

    def test_external_reaper_prevents_unsafe_numeric_signal(self):
        process = mock.Mock(pid=7654321)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=ChildProcessError), mock.patch.object(subject.os, "killpg") as kill:
            with self.assertRaises(ChildProcessError):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        kill.assert_not_called(); process.wait.assert_not_called()

    def test_interruption_closes_the_still_owned_group(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = -9
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=KeyboardInterrupt), mock.patch.object(subject.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        kill.assert_called_once_with(process.pid, subject.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_many_small_tar_rows_hit_cumulative_budget(self):
        raw = tar_bytes([(f"file{i}", b"data", {}) for i in range(10)])
        budget = subject.MetadataBudget(512)
        with self.assertRaises(subject.original.RecoveryError):
            subject.tar_metadata(io.BytesIO(raw), self.root, budget=budget)
        self.assertLessEqual(budget.used, budget.limit)

    def test_valid_wheel_rows_hit_cumulative_budget_before_retention(self):
        budget = subject.MetadataBudget(512)
        with mock.patch.object(subject, "MetadataBudget", return_value=budget):
            with self.assertRaises(subject.original.RecoveryError):
                subject.inspect_wheel(wheel_bytes(), "example-1.0-py3-none-any.whl", self.root)
        self.assertLessEqual(budget.used, budget.limit)

    def test_fixed_manifest_binds_all66_names_and139_payloads(self):
        path = Path(__file__).parents[1] / "ci/vs_host_binary_observation.json"
        value = subject.load_manifest(path)
        self.assertEqual(len(value["inputs"]), 139)
        self.assertEqual(len(value["required_names"]), 66)
        self.assertEqual({r["required_name"] for r in value["inputs"] + value["recovered"]},
                         {r["name"] for r in value["required_names"]})
        self.assertEqual(value["configuration"]["values"]["INCLUDE_FIPS"], "y")

    def test_fixed_manifest_hash_rejects_changed_scope(self):
        path = self.root / "manifest.json"; path.write_text("{}")
        with self.assertRaises(subject.original.RecoveryError):
            subject.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
