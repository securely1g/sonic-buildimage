"""Opaque-byte transport fixtures; no valid package or image is generated."""
import copy
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import promote_vs_original_members as subject


class OriginalMemberPromotionTest(unittest.TestCase):
    def row(self, payload=b"0123456789abcd", kind="wheel"):
        name = "sample-1.0-py3-none-any.whl" if kind == "wheel" else "docker-example.gz"
        target = "target/python-wheels/trixie/" + name if kind == "wheel" else "target/" + name
        return {"kind": kind, "required_name": name, "target_relative_path": target,
                "artifact_id": subject.HOST_ARTIFACT if kind == "wheel" else subject.LEGACY_ARTIFACT,
                "cache_name": name + ".tgz", "cache_bytes": 1, "cache_sha256": "a" * 64,
                "member_path": target, "member_bytes": len(payload),
                "member_sha256": hashlib.sha256(payload).hexdigest()}

    def test_unsealed_selection_does_not_open_any_input(self):
        with patch.object(subject, "SELECTION_SHA256", None), patch.object(subject.original, "HeldFile") as held:
            with self.assertRaisesRegex(subject.original.RecoveryError, "not sealed"):
                subject.load_selection(Path("/unopened-selection.json"))
        held.assert_not_called()

    def test_copy_preserves_odd_length_bytes_and_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            before = source.stat(); output = root / "output"; output.mkdir()
            row = self.row(); result = subject.copy_original(source, row, output)
            self.assertEqual((output / row["target_relative_path"]).read_bytes(), source.read_bytes())
            self.assertEqual(result["copied"]["bytes"], 14)
            self.assertEqual(result["copied"]["sha256"], row["member_sha256"])
            self.assertEqual((before.st_ino, before.st_mtime_ns), (source.stat().st_ino, source.stat().st_mtime_ns))
            self.assertFalse(result["source_admission"]); self.assertFalse(result["VS_assembly_admission"])

    def test_existing_output_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            row = self.row(); output = root / "output"; target = output / row["target_relative_path"]
            target.parent.mkdir(parents=True); target.write_bytes(b"preserve existing")
            with self.assertRaises(FileExistsError): subject.copy_original(source, row, output)
            self.assertEqual(target.read_bytes(), b"preserve existing")

    def test_output_write_failure_propagates_without_source_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            output = root / "output"; output.mkdir()
            with patch.object(subject.original, "copy_member", side_effect=OSError("fixture output failure")):
                with self.assertRaises(OSError): subject.copy_original(source, self.row(), output)
            self.assertEqual(source.read_bytes(), b"0123456789abcd")

    def test_wrong_original_hash_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            output = root / "output"; output.mkdir(); row = self.row(); row["member_sha256"] = "0" * 64
            with self.assertRaisesRegex(subject.original.RecoveryError, "hash differs"):
                subject.copy_original(source, row, output)

    def test_source_mutation_during_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            output = root / "output"; output.mkdir(); copier = subject.original.copy_member
            def mutate(*args):
                result = copier(*args); source.write_bytes(b"changed source"); return result
            with patch.object(subject.original, "copy_member", side_effect=mutate):
                with self.assertRaisesRegex(subject.original.RecoveryError, "identity or metadata changed"):
                    subject.copy_original(source, self.row(), output)

    def test_existing_tgz_owner_copies_only_selected_unchanged_member(self):
        payload = b"0123456789abcd"; row = self.row(payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); artifact = root / "artifact"; (artifact / "execution").mkdir(parents=True)
            scratch = root / "scratch"; scratch.mkdir(); output = root / "output"; output.mkdir()
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, content in [(row["member_path"], payload), ("unselected-note", b"not copied")]:
                    item = tarfile.TarInfo(name); item.size = len(content); archive.addfile(item, io.BytesIO(content))
            raw = gzip.compress(data.getvalue(), mtime=0)
            row["cache_bytes"] = len(raw); row["cache_sha256"] = hashlib.sha256(raw).hexdigest()
            (artifact / "execution" / row["cache_name"]).write_bytes(raw)
            result = subject.promote([row], {subject.HOST_ARTIFACT: artifact}, output, scratch)
            self.assertEqual(len(result), 1)
            self.assertEqual((output / row["target_relative_path"]).read_bytes(), payload)
            self.assertEqual([p for p in output.rglob("*") if p.is_file()], [output / row["target_relative_path"]])
            self.assertEqual(list(scratch.iterdir()), [])

    def test_selection_must_match_original_tuple_and_destination(self):
        host, old = self.row(), self.row(kind="docker")
        selection = {"entries": [copy.deepcopy(host), copy.deepcopy(old)], "selected_member_count": 2,
                     "selected_member_bytes": 28}
        for key, value in [("member_sha256", "0" * 64), ("target_relative_path", "target/changed.whl")]:
            with self.subTest(key=key):
                changed = copy.deepcopy(selection); changed["entries"][0][key] = value
                with self.assertRaises(subject.original.RecoveryError):
                    subject.resolve_rows(changed, {"inputs": [host]}, {"rows": [old]})

    def test_cli_refuses_existing_output_without_writing_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / "output"; output.mkdir()
            marker = output / "keep"; marker.write_bytes(b"original")
            argv = ["promote", "--selection", str(root / "selection"),
                    "--host-roster", str(root / "host.json"), "--legacy-roster", str(root / "legacy.json"),
                    "--companion-roster", str(root / "companions.json"),
                    "--host-artifact", str(root), "--legacy-artifact", str(root),
                    "--output", str(output), "--scratch", str(root / "scratch"),
                    "--report", str(output / "lineage.json")]
            with patch.object(subject.sys, "argv", argv): self.assertEqual(subject.main(), 1)
            self.assertEqual(list(output.iterdir()), [marker])
            self.assertEqual(marker.read_bytes(), b"original")

    def test_later_group_failure_keeps_only_completed_original_groups(self):
        host, old = self.row(), self.row(kind="docker")
        completed = {"name": host["required_name"], "target_relative_path": host["target_relative_path"]}
        report = {"results": [], "status": "ORIGINAL_MEMBER_PROMOTION_INCOMPLETE"}
        with patch.object(subject.binaries, "inspect_cached", side_effect=[
                [completed], subject.original.RecoveryError("fixture later group failure")]) as reader:
            with self.assertRaises(subject.original.RecoveryError):
                subject.promote([host, old], {subject.HOST_ARTIFACT: Path("/unused/host"),
                    subject.LEGACY_ARTIFACT: Path("/unused/legacy")}, Path("/unused/output"),
                    Path("/unused/scratch"), report)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(report["results"], [completed])
        self.assertEqual(report["active_original_object"], {"artifact_id": subject.LEGACY_ARTIFACT,
            "cache_name": old["cache_name"], "cache_bytes": old["cache_bytes"],
            "cache_sha256": old["cache_sha256"]})
        self.assertEqual(report["status"], "ORIGINAL_MEMBER_PROMOTION_INCOMPLETE")

    def test_sibling_selection_requires_the_separate_fixed_roster(self):
        host, old = self.row(), self.row(kind="docker")
        companion = dict(host, kind="deb", required_name="core_0.142_all.deb",
            target_relative_path="target/debs/trixie/core_0.142_all.deb",
            member_path="target/debs/trixie/core_0.142_all.deb", cache_name="core.tgz")
        selection = {"entries": [host, companion, old], "selected_member_count": 3, "selected_member_bytes": 42}
        original_host = {"inputs": [copy.deepcopy(host)]}
        with self.assertRaises(subject.original.RecoveryError):
            subject.resolve_rows(selection, original_host, {"rows": [old]})
        result = subject.resolve_rows(selection, original_host, {"rows": [old]}, {"inputs": [companion]})
        self.assertEqual(result, [host, companion, old])
        self.assertEqual(original_host, {"inputs": [host]})

    def test_legacy_copy_records_owner_metadata_without_promoting_loader_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "original"; source.write_bytes(b"0123456789abcd")
            output = root / "output"; output.mkdir(); row = self.row(kind="docker")
            row.update(expected_tags=["docker-example:latest"], entrypoint=["/entry"])
            facts = {"make_loader_observation": {"latest_tag_present": False}, "source_admission": False}
            with patch.object(subject.legacy.archives, "observe", return_value=facts) as inspect:
                result = subject.copy_original(source, row, output)
            self.assertEqual(result["archive_configuration_observation"], facts)
            self.assertTrue(inspect.call_args.kwargs["include_loader_metadata"])
            self.assertTrue(inspect.call_args.kwargs["include_repo_tags"])
            self.assertEqual(inspect.call_args.kwargs["expected_sha256"], row["member_sha256"])
            self.assertFalse(result["VS_assembly_admission"])
            self.assertEqual((output / row["target_relative_path"]).read_bytes(), source.read_bytes())

    def test_fixed_original_rows_resolve_without_metadata_rewriting(self):
        host, old = self.row(), self.row(kind="docker")
        selection = {"entries": [copy.deepcopy(host), copy.deepcopy(old)], "selected_member_count": 2,
                     "selected_member_bytes": 28}
        result = subject.resolve_rows(selection, {"inputs": [host]}, {"rows": [old]})
        self.assertEqual(result, [host, old])


if __name__ == "__main__":
    unittest.main()
