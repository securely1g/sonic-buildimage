"""Small in-memory ZIP/stream fixtures; no network, project build or DEBs."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import warnings
import urllib.error
import zipfile

from tools.bazel.ci import native_payload_inventory as inventory


class NativePayloadInventoryTest(unittest.TestCase):
    def zipped(self, entries):
        stream = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, body in entries:
                    archive.writestr(name, body)
        raw = stream.getvalue()
        return raw, {"id": 13803477455, "size_in_bytes": len(raw),
                     "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}

    def test_complete_inventory_hashes_directories_and_files(self):
        raw, artifact = self.zipped([("public/", b""), ("public/receipt.json", b'{"original":true}\n'),
                                    ("image.tar", b"literal original payload fixture")])
        result = inventory.inventory(io.BytesIO(raw), artifact, float("inf"))
        self.assertTrue(result["all_members_crc_verified"])
        self.assertEqual(result["members"][0]["sha256"], None)
        self.assertEqual(result["members"][1]["sha256"], hashlib.sha256(b'{"original":true}\n').hexdigest())
        self.assertEqual(result["zip_sha256"], artifact["digest"][7:])

    def test_stream_accepts_exact_bytes_and_hash_without_large_reads(self):
        body = b"x" * (inventory.CHUNK + 17)
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= inventory.CHUNK:
                    raise AssertionError("Unbounded stream read")
                return super().read(size)
        output = io.BytesIO()
        observed = inventory.stream_exact(Bounded(body), output, len(body), hashlib.sha256(body).hexdigest(), float("inf"))
        self.assertEqual(output.getvalue(), body)
        self.assertEqual(observed["bytes"], len(body))

    def test_stream_rejects_short_extra_and_changed_bytes(self):
        expected = b"original"
        for body in (expected[:-1], expected + b"extra", b"tampered"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                inventory.stream_exact(io.BytesIO(body), io.BytesIO(), len(expected),
                                       hashlib.sha256(expected).hexdigest(), float("inf"))

    def test_expired_stream_stops_before_read(self):
        source = mock.Mock()
        with self.assertRaises(ValueError):
            inventory.stream_exact(source, io.BytesIO(), 1, "0" * 64, 0)
        source.read.assert_not_called()

    def test_corrupt_payload_fails_crc(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("receipt.json", b"known-payload-for-corruption")
        raw = stream.getvalue().replace(b"known-payload-for-corruption", b"changed-payload-for-corrupt!")
        self.assertEqual(len(b"known-payload-for-corruption"), len(b"changed-payload-for-corrupt!"))
        artifact = {"id": 13803477455, "size_in_bytes": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        with self.assertRaises(zipfile.BadZipFile):
            inventory.inventory(io.BytesIO(raw), artifact, float("inf"))

    def test_duplicate_and_noncanonical_names_are_rejected(self):
        for entries in ((('same', b'a'), ('same', b'b')), (('../escape', b'a'),),
                        (('/absolute', b'a'),), (('nested//alias', b'a'),)):
            raw, artifact = self.zipped(entries)
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                inventory.inventory(io.BytesIO(raw), artifact, float("inf"))

    def test_symlink_and_special_members_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO):
            entry = zipfile.ZipInfo("unexpected")
            entry.create_system = 3
            entry.external_attr = (mode | 0o600) << 16
            raw, artifact = self.zipped([(entry, b"literal")])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                inventory.inventory(io.BytesIO(raw), artifact, float("inf"))

    def test_file_directory_shadowing_is_rejected(self):
        raw, artifact = self.zipped([("same", b"file"), ("same/child", b"child")])
        with self.assertRaises(ValueError):
            inventory.inventory(io.BytesIO(raw), artifact, float("inf"))

    def test_expansion_member_and_total_bounds_fail_before_payload_reads(self):
        raw, artifact = self.zipped([("a", b"1234"), ("b", b"1234")])
        for name, value in (("MAX_MEMBERS", 1), ("MAX_ZIP_BYTES", 3), ("MAX_EXPANDED_BYTES", 7)):
            with self.subTest(bound=name), mock.patch.object(inventory.recovery, name, value):
                with self.assertRaises(ValueError):
                    inventory.inventory(io.BytesIO(raw), artifact, float("inf"))

    def test_truncated_archive_and_inventory_deadline_fail(self):
        raw, artifact = self.zipped([("a", b"original")])
        with self.assertRaises(zipfile.BadZipFile):
            inventory.inventory(io.BytesIO(raw[:-15]), artifact, float("inf"))
        with self.assertRaises(ValueError):
            inventory.inventory(io.BytesIO(raw), artifact, 0)

    def test_hosting_scope_rejects_credentials_bad_origins_and_fragments(self):
        for url in ("http://example.blob.core.windows.net/x", "https://blob.core.windows.net.attacker.test/x",
                    "https://user:password@example.blob.core.windows.net/x",
                    "https://example.blob.core.windows.net:444/x", "https://example.blob.core.windows.net/x#secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                inventory.hosting_url(url)
        self.assertEqual(inventory.hosting_url("https://example.blob.core.windows.net/x?signature=opaque"),
                         "example.blob.core.windows.net")

    def test_held_directory_refuses_symlinks_and_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(ValueError):
                with inventory.directory(root / "link"):
                    self.fail("Followed symlink")
            with self.assertRaises(ValueError):
                with inventory.directory(root / "real"):
                    (root / "real").rename(root / "preserved")
                    (root / "real").mkdir()

    def test_metadata_write_is_exclusive_and_size_bounded(self):
        for raw in (b'{"same":1,"same":2}', b'{"nonfinite":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                inventory.strict(raw)
        with tempfile.TemporaryDirectory() as temporary:
            root = os.open(temporary, inventory.DIRECTORY_FLAGS)
            try:
                inventory.write_bytes(root, "original.data", b"original", 8)
                with self.assertRaises(FileExistsError):
                    inventory.write_bytes(root, "original.data", b"changed", 8)
                with self.assertRaises(ValueError):
                    inventory.write_bytes(root, "oversize.data", b"too large", 3)
                self.assertFalse((Path(temporary) / "oversize.data").exists())
                self.assertEqual((Path(temporary) / "original.data").read_bytes(), b"original")
            finally:
                os.close(root)

    def test_artifact_redirect_does_not_forward_workflow_authentication(self):
        raw, artifact = self.zipped([("public.json", b"{}")])
        artifact.update(name="source-image-p4rt-42860613563-1",
                        archive_download_url=inventory.API + "/actions/artifacts/13803477455/zip")
        fixed = {"artifact_id": artifact["id"], "artifact_name": artifact["name"],
                 "zip_bytes": len(raw), "zip_sha256": hashlib.sha256(raw).hexdigest()}
        seen = []
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, request, timeout):
                seen.append(request)
                if len(seen) == 1:
                    raise urllib.error.HTTPError(request.full_url, 302, "redirect",
                        {"Location": "https://original.blob.core.windows.net/original?signature=fixture"},
                        io.BytesIO(b""))
                return Response(raw)
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"GH_TOKEN": "fixture-token"}))
            stack.enter_context(mock.patch.object(inventory.urllib.request, "build_opener", return_value=Opener()))
            stack.enter_context(mock.patch.object(inventory, "api_json", side_effect=[
                {"id": 42860613563}, {"id": 129985415221}, artifact]))
            stack.enter_context(mock.patch.object(inventory.recovery, "admit_original", return_value={
                "status": "ORIGINAL_FAILURE_ADMITTED_FOR_INSPECTION_ONLY",
                "original_request": {"image": "p4rt"}}))
            stack.enter_context(mock.patch.object(inventory.os, "fstatvfs", return_value=SimpleNamespace(
                f_bavail=inventory.SPACE_RESERVE + 100000, f_frsize=1)))
            stack.enter_context(mock.patch.object(inventory, "time_budget", side_effect=lambda seconds: contextlib.nullcontext()))
            fd = os.open(temporary, inventory.DIRECTORY_FLAGS)
            try:
                result = inventory.collect(fd, {}, fixed, b"original request fixture", {}, [], {})
            finally:
                os.close(fd)
            self.assertEqual(seen[0].get_header("Authorization"), "Bearer fixture-token")
            self.assertIsNone(seen[1].get_header("Authorization"))
            self.assertIsNone(seen[1].get_header("Cookie"))
            self.assertEqual(len(seen), 2)
            self.assertEqual((Path(temporary) / "original.zip").read_bytes(), raw)
            self.assertFalse(result["canonical_native_acceptance"])
            self.assertFalse(result["original_member_paths_materialized"])
            self.assertNotIn("fixture-token", (Path(temporary) / "member-inventory.json").read_text())
            retained = json.loads((Path(temporary) / "public-evidence-map.json").read_text())
            self.assertEqual(len(retained["members"]), 1)
            row = retained["members"][0]
            self.assertEqual(row["original_member"], "public.json")
            self.assertEqual(row["retained_file"], "public-evidence-0000.data")
            original = (Path(temporary) / row["retained_file"]).read_bytes()
            self.assertEqual(original, b"{}")
            self.assertEqual(hashlib.sha256(original).hexdigest(), row["sha256"])
            self.assertEqual(result["public_JSON_files_retained"], 1)
            self.assertEqual(result["payload_bytes_retained_outside_ZIP"], 0)

    def test_public_json_roundtrip_preserves_bytes_and_uses_exact_root_paths(self):
        original = b'{ "source" : "original", "schema": 1 }\n'
        lock = b'{"lockFileVersion":3,"registryFileHashes":{},"moduleExtensions":{}}'
        raw, artifact = self.zipped([("receipt.json", original), ("MODULE.bazel.lock", lock),
                                    ("image.tar", b"payload remains in original ZIP")])
        public = {}
        result = inventory.inventory(io.BytesIO(raw), artifact, float("inf"), public)
        self.assertEqual(set(public), {"receipt.json", "MODULE.bazel.lock"})
        self.assertEqual(public["receipt.json"]["raw"], original)
        self.assertEqual(public["MODULE.bazel.lock"]["raw"], lock)
        self.assertEqual(public["MODULE.bazel.lock"]["validator"], "dependency_json:lock")
        self.assertEqual(public["receipt.json"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(len(result["members"]), 3)

    def test_public_json_safety_and_nested_layout_fail_closed(self):
        for entries in ([("receipt.json", b'{"token":"fixture-secret"}')],
                        [("nested/receipt.json", b"{}")], [("image.tar", b"payload only")]):
            raw, artifact = self.zipped(entries)
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                inventory.inventory(io.BytesIO(raw), artifact, float("inf"), {})

    def test_public_json_member_and_aggregate_limits(self):
        raw, artifact = self.zipped([("one.json", b"{}"), ("two.json", b"{}")])
        for name, value in (("PUBLIC_MEMBER_BYTES", 1), ("PUBLIC_TOTAL_BYTES", 3)):
            with self.subTest(bound=name), mock.patch.object(inventory, name, value):
                with self.assertRaises(ValueError):
                    inventory.inventory(io.BytesIO(raw), artifact, float("inf"), {})



    def test_public_json_exact_retention_boundaries_and_one_byte_overflow(self):
        lock = b'{"lockFileVersion":3,"registryFileHashes":{},"moduleExtensions":{}}'
        body, artifact = self.zipped([("MODULE.bazel.lock", lock), ("receipt.json", b"{}")])
        with mock.patch.object(inventory, "PUBLIC_MEMBER_BYTES", len(lock)), \
             mock.patch.object(inventory, "PUBLIC_TOTAL_BYTES", len(lock) + 2):
            public = {}
            inventory.inventory(io.BytesIO(body), artifact, float("inf"), public)
            self.assertEqual(public["MODULE.bazel.lock"]["raw"], lock)
            self.assertEqual(public["receipt.json"]["raw"], b"{}")
        for name, limit in (("PUBLIC_MEMBER_BYTES", len(lock) - 1),
                            ("PUBLIC_TOTAL_BYTES", len(lock) + 1)):
            with self.subTest(bound=name), mock.patch.object(inventory, name, limit):
                with self.assertRaisesRegex(ValueError, "Original public JSON retention bound exceeded"):
                    inventory.inventory(io.BytesIO(body), artifact, float("inf"), {})

    def test_original_lock_above_old_retention_cap_roundtrips_unchanged(self):
        lock = (b'{"lockFileVersion":3,"registryFileHashes":{},"moduleExtensions":{},'
                b'"facts":{"fixture":"' + b"x" * (2 * 1024**2) + b'"}}')
        body, artifact = self.zipped([("MODULE.bazel.lock", lock), ("receipt.json", b"{}")])
        public = {}
        result = inventory.inventory(io.BytesIO(body), artifact, float("inf"), public)
        self.assertTrue(result["all_members_crc_verified"])
        self.assertEqual(public["MODULE.bazel.lock"]["raw"], lock)
        self.assertEqual(public["MODULE.bazel.lock"]["sha256"], hashlib.sha256(lock).hexdigest())
        self.assertEqual(public["MODULE.bazel.lock"]["validator"], "dependency_json:lock")
        self.assertEqual(public["receipt.json"]["raw"], b"{}")

    def test_large_lock_keeps_schema_duplicate_key_and_safety_refusals(self):
        prefix = (b'{"lockFileVersion":3,"registryFileHashes":{},"moduleExtensions":{},'
                  b'"facts":{"fixture":"' + b"x" * (2 * 1024**2) + b'"}')
        for suffix in (b',"unknown":{}}', b',"facts":{}}', b',"facts":{"token":"fixture-secret"}}'):
            # A credential field is also checked without relying on duplicate-key rejection.
            lock = prefix + suffix if b'token' not in suffix else prefix.replace(b'"fixture":', b'"token":') + b'}'
            body, artifact = self.zipped([("MODULE.bazel.lock", lock)])
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                inventory.inventory(io.BytesIO(body), artifact, float("inf"), {})


    def test_fixed_original_inputs_preserve_each_family_request_and_contract(self):
        self.assertEqual(set(inventory.INPUTS), {"p4rt", "syncd", "gearbox"})
        for family in inventory.INPUTS:
            with self.subTest(family=family):
                fixed, raw, contract = inventory.read_input(family)
                request, case = inventory.recovery.original_request(raw, contract)
                self.assertEqual(inventory.recovery.worker.image(request), family)
                self.assertEqual(fixed["original_run"], case["run"])
                self.assertEqual(fixed["original_job"], case["job"])
                self.assertEqual(fixed["artifact_id"], case["artifact"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case["request_sha256"])

    def test_fixed_family_metadata_requests_use_only_the_selected_original_ids(self):
        class Response(io.BytesIO):
            status = 200
        for family, case in inventory.recovery.CASES.items():
            for plural, key in (("runs", "run"), ("jobs", "job"), ("artifacts", "artifact")):
                with self.subTest(family=family, kind=plural), contextlib.ExitStack() as stack:
                    opener = mock.MagicMock()
                    opener.open.return_value = Response(json.dumps({"id": case[key]}).encode())
                    stack.enter_context(mock.patch.object(inventory, "write_bytes"))
                    stack.enter_context(mock.patch.object(inventory, "write_json"))
                    stack.enter_context(mock.patch.object(inventory, "time_budget", side_effect=lambda seconds: contextlib.nullcontext()))
                    suffix = "/actions/" + plural + "/" + str(case[key])
                    value = inventory.api_json(opener, "fixture-token", suffix, 0, "fixture", [], family)
                    self.assertEqual(value, {"id": case[key]})
                    request = opener.open.call_args.args[0]
                    self.assertEqual(request.full_url, inventory.API + suffix)
                    self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")

    def test_unknown_or_cross_family_selection_is_rejected_before_io(self):
        opener = mock.MagicMock()
        with mock.patch.object(inventory.os, "open") as open_file:
            with self.assertRaises(ValueError):
                inventory.read_input("../arbitrary")
            open_file.assert_not_called()
        with self.assertRaises(ValueError):
            inventory.api_json(opener, "fixture-token", "/actions/runs/42860613563", 0, "fixture", [], "syncd")
        opener.open.assert_not_called()
        with self.assertRaises(ValueError):
            inventory.collect(0, {}, {"artifact_id": 13803477455}, b"unused", {}, [], {}, "gearbox")


class TimerRestorationTest(unittest.TestCase):
    def test_expired_outer_timer_is_disabled_and_original_failure_preserved(self):
        for explicit in (False, True):
            original = TimeoutError("original owned deadline")
            with self.subTest(explicit=explicit), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(inventory.time, "monotonic", side_effect=[10, 12]))
                stack.enter_context(mock.patch.object(inventory.signal, "getitimer", return_value=(1, 0)))
                setter = stack.enter_context(mock.patch.object(inventory.signal, "setitimer"))
                with self.assertRaises(TimeoutError) as caught:
                    with inventory.time_budget(900):
                        if explicit:
                            raise original
                if explicit:
                    self.assertIs(caught.exception, original)
                self.assertEqual(setter.call_args_list, [
                    mock.call(inventory.signal.ITIMER_REAL, 1), mock.call(inventory.signal.ITIMER_REAL, 0)])

    def test_positive_outer_budget_is_restored_after_phase_failure(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(inventory.time, "monotonic", side_effect=[10, 12]))
            stack.enter_context(mock.patch.object(inventory.signal, "getitimer", return_value=(30, 0)))
            setter = stack.enter_context(mock.patch.object(inventory.signal, "setitimer"))
            original = ValueError("phase failure")
            with self.assertRaises(ValueError) as caught:
                with inventory.time_budget(5):
                    raise original
            self.assertIs(caught.exception, original)
            self.assertEqual(setter.call_args_list, [
                mock.call(inventory.signal.ITIMER_REAL, 5), mock.call(inventory.signal.ITIMER_REAL, 28)])


class RetainedPublicSemanticReviewTest(unittest.TestCase):
    def fixture(self):
        values = {"public-receipt.json": b'{"artifacts":{}}', "worker.json": b'{"original":true}',
                  "extra-safe.json": b'{"retained":true}', "MODULE.bazel.lock": b'{}'}
        public = {name: {"raw": raw, "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in values.items()}
        retained = [{"original_member": name, "retained_file": f"public-evidence-{i:04d}.data",
                     "bytes": len(entry["raw"]), "sha256": entry["sha256"]}
                    for i, (name, entry) in enumerate(sorted(public.items()))]
        return public, retained

    def test_review_uses_original_bytes_exact_names_and_only_canonical_subset(self):
        public, retained = self.fixture()
        artifact = {"id": 13803477455, "digest": "sha256:" + "a" * 64}
        contract, request, run, job, observed = {"outputs": {"image.tar": "//:image"}}, b"original", {}, {}, {}
        names = frozenset(("public-receipt.json", "worker.json"))
        reviewed = {"status": "PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED", "canonical_native_acceptance": False}
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(inventory.recovery.worker, "public_json_names", return_value=names))
            verifier = stack.enter_context(mock.patch.object(inventory.recovery, "review_public_upload", return_value=reviewed))
            fd = os.open(temporary, inventory.DIRECTORY_FLAGS)
            try:
                for row in retained:
                    inventory.write_bytes(fd, row["retained_file"], public[row["original_member"]]["raw"])
                result = inventory.public_review(fd, request, contract, run, job, artifact, observed,
                                                 public, retained, {"original_request": {"image": "p4rt"}})
            finally:
                os.close(fd)
            selected = {name: public[name]["raw"] for name in names}
            verifier.assert_called_once_with(request, contract, run, job, artifact, observed, selected,
                                             {name: name for name in set(names) | {"image.tar"}})
            self.assertEqual(result["original_conclusion"], "failure")
            self.assertFalse(result["canonical_native_acceptance"])
            self.assertFalse(result["PR206_adoption"])
            self.assertEqual(json.loads((Path(temporary) / "public-semantic-review.json").read_text()), result)
            for row in retained:
                self.assertEqual((Path(temporary) / row["retained_file"]).read_bytes(), public[row["original_member"]]["raw"])

    def test_changed_retained_hash_stops_before_semantic_validator(self):
        public, retained = self.fixture(); retained[0]["sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(inventory.recovery, "review_public_upload") as verifier:
            fd = os.open(temporary, inventory.DIRECTORY_FLAGS)
            try:
                result = inventory.public_review(fd, b"original", {}, {}, {},
                    {"id": 13803477455, "digest": "sha256:" + "a" * 64}, {}, public, retained, {})
            finally:
                os.close(fd)
            verifier.assert_not_called()
            self.assertEqual(result["status"], "PUBLIC_SEMANTICS_REJECTED_INSPECTION_ONLY")
            self.assertFalse(result["canonical_native_acceptance"])

    def test_semantic_rejection_is_bounded_and_cannot_turn_original_failure_into_acceptance(self):
        public, retained = self.fixture()
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(inventory.recovery.worker, "public_json_names", return_value=frozenset(("worker.json",))))
            stack.enter_context(mock.patch.object(inventory.recovery, "review_public_upload", side_effect=ValueError("private free-form detail")))
            fd = os.open(temporary, inventory.DIRECTORY_FLAGS)
            try:
                result = inventory.public_review(fd, b"original", {"outputs": {}}, {}, {},
                    {"id": 13803477455, "digest": "sha256:" + "a" * 64}, {}, public, retained, {"original_request": {}})
            finally:
                os.close(fd)
            self.assertEqual(result["status"], "PUBLIC_SEMANTICS_REJECTED_INSPECTION_ONLY")
            self.assertEqual(result["error_type"], "ValueError")
            self.assertFalse(result["canonical_native_acceptance"])
            self.assertFalse(result["fresh_build_or_runtime"])
            self.assertNotIn("private free-form", (Path(temporary) / "public-semantic-review.json").read_text())

if __name__ == "__main__":
    unittest.main()
