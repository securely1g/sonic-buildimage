"""Checks that installed-image APT evidence cannot hide incomplete package state."""

import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import validate_dpkg_state as validation


class PackageStateValidationTest(unittest.TestCase):
    def test_apt_must_not_restore_excluded_baseline_plugins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = 'usr/lib/x86_64-linux-gnu/libnl/cli/cls/basic.so'
            receipt = {'source_packages': [{'excluded_baseline_paths': [name]}]}
            self.assertEqual(validation.source_file_inventory([receipt], root), {})
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_bytes(b'unused Debian plugin')
            with self.assertRaisesRegex(ValueError, 'Excluded baseline file'):
                validation.source_file_inventory([receipt], root)
            path.unlink()
            path.symlink_to('missing.so')
            with self.assertRaisesRegex(ValueError, 'Excluded baseline file'):
                validation.source_file_inventory([receipt], root)

    def test_installed_records_ignore_removed_but_require_valid_identity(self):
        result = validation.parse_installed(
            "libexample:arm64\tinstall ok installed\t1.2-3\tarm64\n"
            "held\thold ok installed\t2.0\tall\n"
            "former\tdeinstall ok config-files\t1.0\tall\n")
        self.assertEqual(result, {"libexample:arm64": {"version": "1.2-3", "architecture": "arm64"},
                                  "held": {"version": "2.0", "architecture": "all"}})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validation.parse_installed("x\tinstall ok installed\t1\tall\nx\tinstall ok installed\t1\tall\n")

    def test_status_only_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root / "var/lib/dpkg/info"
            info.mkdir(parents=True)
            (info / "present:arm64.list").write_text("/.\n/usr/lib/example.so\n")
            records = {"present:arm64": {"version": "1", "architecture": "arm64"},
                       "missing": {"version": "1", "architecture": "arm64"}}
            with self.assertRaisesRegex(ValueError, "ownership list: missing"):
                validation.info_inventory(records, root)
            (info / "missing.list").write_text("/.\n")
            self.assertEqual(validation.info_inventory(records, root)["present:arm64"]["owned_paths"], 2)

    def test_ownership_list_cannot_escape_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root / "var/lib/dpkg/info"
            info.mkdir(parents=True)
            (info / "example.list").write_text("/usr/../../outside\n")
            with self.assertRaisesRegex(ValueError, "Invalid owned path"):
                validation.info_inventory({"example": {"version": "1", "architecture": "all"}}, root)

    def test_receipt_detects_missing_and_changed_maintainer_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "var/lib/dpkg/info/example.postinst"
            path = root / name
            path.parent.mkdir(parents=True)
            content = b"#!/bin/sh\nset -e\nldconfig\n"
            digest = hashlib.sha256(content).hexdigest()
            receipt = {"dpkg_state": {"packages": {"example": {"info_files": {name: {"sha256": digest}}}}}}
            with self.assertRaisesRegex(ValueError, "differs from build receipt"):
                validation.verify_receipt_files([receipt], root)
            path.write_bytes(content)
            self.assertEqual(validation.verify_receipt_files([receipt], root), {name: digest})
            path.write_text("#!/bin/sh\ntrue\n")
            with self.assertRaisesRegex(ValueError, "differs from build receipt"):
                validation.verify_receipt_files([receipt], root)

    def test_receipt_checks_script_executable_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "var/lib/dpkg/info/example.postinst"
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\ntrue\n")
            path.chmod(0o644)
            receipt = {"info_files": {name: {"sha256": validation.sha(path), "mode": 0o755}}}
            with self.assertRaisesRegex(ValueError, "mode differs"):
                validation.verify_receipt_files([receipt], root)
            path.chmod(0o755)
            validation.verify_receipt_files([receipt], root)

    def test_receipts_do_not_silently_accept_unsupported_shape_or_conflicting_hashes(self):
        with self.assertRaisesRegex(ValueError, "no dpkg state file hashes"):
            validation.verify_receipt_files([{"source_packages": []}])
        name = "var/lib/dpkg/info/x.list"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            validation.verify_receipt_files([{"info_files": {name: "a" * 64}}, {"info_files": {name: "b" * 64}}])

    def test_receipt_requires_whiteout_targets_to_be_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "var/lib/dpkg/status"
            status = root / name
            status.parent.mkdir(parents=True)
            status.write_text("installed state\n")
            removed = root / "usr/bin/former-command"
            removed.parent.mkdir(parents=True)
            receipt = {"info_files": {name: validation.sha(status)}, "removed_paths": ["usr/bin/former-command"]}
            validation.verify_receipt_files([receipt], root)
            removed.symlink_to("missing")
            with self.assertRaisesRegex(ValueError, "Obsolete package file"):
                validation.verify_receipt_files([receipt], root)
            receipt["removed_paths"] = ["../outside"]
            with self.assertRaisesRegex(ValueError, "Invalid removed"):
                validation.verify_receipt_files([receipt], root)

    def test_both_native_architectures_have_a_command_and_new_library(self):
        path = Path(__file__).with_name("dpkg_transactions.lock.json")
        for architecture in ("amd64", "arm64"):
            value = validation.read_lock(path, architecture)
            self.assertEqual(value["package"], "qrencode")
            self.assertEqual(value["required_dependencies"], ["libqrencode4"])
            self.assertTrue(all(architecture + ".deb" in entry["url"] for entry in value["packages"][architecture]))

    def test_lock_rejects_untrusted_host_and_missing_dependency(self):
        source = json.loads(Path(__file__).with_name("dpkg_transactions.lock.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            original = source["packages"]["amd64"][0]["url"]
            source["packages"]["amd64"][0]["url"] = "https://example.invalid/qrencode.deb"
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, "official Debian"):
                validation.read_lock(path, "amd64")
            source["packages"]["amd64"][0]["url"] = original
            source["required_dependencies"] = ["missing-library"]
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, "dependencies are absent"):
                validation.read_lock(path, "amd64")

    def test_runtime_layer_files_are_protected_without_a_dpkg_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "usr/local/lib/python3.13/site-packages/example.py"
            path.parent.mkdir(parents=True)
            path.write_text("original wheel contents\n")
            entry = {"path": str(path.relative_to(root)), "sha256": validation.sha(path),
                     "mode": path.stat().st_mode & 0o7777, "uid": path.stat().st_uid, "gid": path.stat().st_gid}
            receipt = {"source_packages": [{"package": "sonic-wheel", "packaging": "runtime-layer",
                                            "runtime_files": [entry]}]}
            self.assertIn(entry["path"], validation.source_file_inventory([receipt], root))
            path.write_text("overwritten by apt\n")
            with self.assertRaisesRegex(ValueError, "Source payload differs"):
                validation.source_file_inventory([receipt], root)

    def test_corrupt_cached_package_is_rejected_before_dpkg_or_container_execution(self):
        lock = validation.read_lock(Path(__file__).with_name("dpkg_transactions.lock.json"), "amd64")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "qrencode_4.1.1-2_amd64.deb").write_bytes(b"not a Debian package")
            with mock.patch.object(validation, "host_command") as command:
                with self.assertRaisesRegex(ValueError, "Cached Debian package differs"):
                    validation.prepare_repository(lock, "amd64", repository)
                command.assert_not_called()

    def test_source_soname_links_are_protected_after_apt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'usr/lib/libexample.so.1'
            path.parent.mkdir(parents=True)
            path.symlink_to('libexample.so.1.2.3')
            metadata = path.lstat()
            entry = {'path': str(path.relative_to(root)), 'kind': 'symlink',
                     'linkname': 'libexample.so.1.2.3', 'mode': 0o777,
                     'uid': metadata.st_uid, 'gid': metadata.st_gid}
            receipt = {'source_packages': [{'package': 'example', 'runtime_links': [entry]}]}
            self.assertIn(entry['path'], validation.source_file_inventory([receipt], root))
            path.unlink()
            path.symlink_to('libexample.so.1.9.9')
            with self.assertRaisesRegex(ValueError, 'Source symlink differs'):
                validation.source_file_inventory([receipt], root)

    def test_flattened_symlink_dot_prefix_preserves_target_and_metadata_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'usr/lib/libexample.so.1'
            path.parent.mkdir(parents=True)
            path.symlink_to('./libexample.so.1.2.3')
            metadata = path.lstat()
            entry = {'path': str(path.relative_to(root)), 'kind': 'symlink',
                     'linkname': 'libexample.so.1.2.3', 'mode': 0o777,
                     'uid': metadata.st_uid, 'gid': metadata.st_gid}
            receipt = {'source_packages': [{'package': 'example', 'runtime_links': [entry]}]}
            self.assertIn(entry['path'], validation.source_file_inventory([receipt], root))
            for key, value in [('uid', metadata.st_uid + 1), ('gid', metadata.st_gid + 1), ('mode', 0o755)]:
                with self.subTest(changed=key):
                    changed = {'source_packages': [{'runtime_links': [dict(entry, **{key: value})]}]}
                    with self.assertRaisesRegex(ValueError, 'Source symlink differs'):
                        validation.source_file_inventory([changed], root)
            path.unlink()
            path.symlink_to('./../libexample.so.1.2.3')
            with self.assertRaisesRegex(ValueError, 'Source symlink differs'):
                validation.source_file_inventory([receipt], root)

    def test_install_checks_new_dependencies_and_preserves_existing_packages(self):
        lock = validation.read_lock(Path(__file__).with_name("dpkg_transactions.lock.json"), "amd64")
        before = {"libc6:amd64": {"version": "2.41", "architecture": "amd64"}}
        added = {row["package"]: {"version": row["version"], "architecture": "amd64"}
                 for row in lock["packages"]["amd64"]}
        for preserve_existing in (True, False):
            after = dict(before, **added)
            if not preserve_existing:
                after["libc6:amd64"] = {"version": "unexpected replacement", "architecture": "amd64"}
            with self.subTest(preserve_existing=preserve_existing), \
                    mock.patch.object(validation.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(validation, "guest_command", return_value=subprocess.CompletedProcess([], 0, "amd64\n", "")) as execute, \
                    mock.patch.object(validation, "installed_records", side_effect=[before, after]), \
                    mock.patch.object(validation, "info_inventory", return_value={}), \
                    mock.patch.object(validation, "verify_receipt_files", return_value={}), \
                    mock.patch.object(validation, "source_file_inventory", return_value={}), \
                    mock.patch.object(validation, "audit"), \
                    mock.patch.object(validation, "run_runtime_smoke") as smoke, \
                    mock.patch.object(validation.Path, "mkdir"):
                result = validation.run_guest({"mode": "install", "architecture": "amd64", "lock": lock})
                self.assertEqual(result["status"], "passed" if preserve_existing else "failed")
                commands = [call.args[0] for call in execute.call_args_list]
                self.assertTrue(any(command[-2:] == ["install", "qrencode"] for command in commands))
                self.assertFalse(any(argument in ("remove", "purge", "upgrade") for command in commands for argument in command))
                if preserve_existing:
                    self.assertEqual(smoke.call_count, 2)
                else:
                    self.assertIn("changed an existing package", result["error"])

    def run_install_with_inherited_sources(self, *, mutation=None, conflicting_source=False,
                                           stale_current_state=False):
        """Use real receipt/filesystem checks around a simulated APT transaction."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def source_file(name):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("source bytes for " + name)
                metadata = path.stat()
                return {"path": name, "sha256": validation.sha(path), "mode": metadata.st_mode & 0o7777,
                        "uid": metadata.st_uid, "gid": metadata.st_gid}

            base_file = source_file("usr/lib/libcrypto.so.3")
            common_file = source_file("usr/lib/libswsscommon.so.0")
            service_file = source_file("usr/bin/orchagent")
            link = root / "usr/lib/libswsscommon.so"
            link.symlink_to("libswsscommon.so.0")
            link_metadata = link.lstat()
            common_link = {"path": str(link.relative_to(root)), "kind": "symlink", "linkname": "libswsscommon.so.0",
                           "mode": 0o777, "uid": link_metadata.st_uid, "gid": link_metadata.st_gid}
            state_name = "var/lib/dpkg/status"
            state = root / state_name
            state.parent.mkdir(parents=True)
            state.write_text("current service dpkg state\n")
            current_state = validation.sha(state)
            common = {"package": "libswsscommon", "runtime_files": [common_file], "runtime_links": [common_link]}
            receipts = {
                "base": {"schema": 1, "architecture": "amd64", "info_files": {state_name: "a" * 64},
                         "source_packages": [{"package": "libssl3t64", "runtime_files": [base_file]}]},
                "config-engine": {"schema": 1, "architecture": "amd64", "info_files": {state_name: "b" * 64},
                                  "source_packages": [common]},
                "orchagent": {"schema": 1, "architecture": "amd64",
                              "info_files": {state_name: "c" * 64 if stale_current_state else current_state},
                              "source_packages": [common, {"package": "swss", "runtime_files": [service_file]}]},
            }
            if conflicting_source:
                receipts["config-engine"]["source_packages"] = [dict(common, runtime_files=[dict(common_file, sha256="d" * 64)])]
            paths = {}
            for name, receipt in receipts.items():
                paths[name] = "/usr/share/sonic/bazel/" + name + "-packages.json"
                path = root / paths[name].lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(receipt))
            lock = validation.read_lock(Path(__file__).with_name("dpkg_transactions.lock.json"), "amd64")
            before = {"libc6:amd64": {"version": "2.41", "architecture": "amd64"}}
            after = dict(before, **{row["package"]: {"version": row["version"], "architecture": "amd64"}
                                    for row in lock["packages"]["amd64"]})
            commands = []

            def execute(command, _commands):
                commands.append(command)
                if command[-2:] == ["install", "qrencode"]:
                    if mutation == "file":
                        (root / base_file["path"]).write_text("Debian replacement\n")
                    elif mutation == "file-to-link":
                        path = root / base_file["path"]
                        target = path.with_name("same-bytes.so")
                        path.rename(target)
                        path.symlink_to(target.name)
                    elif mutation == "link":
                        link.unlink()
                        link.symlink_to("libswsscommon.so.other")
                return subprocess.CompletedProcess(command, 0, "amd64\n", "")

            load_receipts = validation.load_installed_receipts
            verify_receipts = validation.verify_receipt_files
            source_inventory = validation.source_file_inventory
            with mock.patch.object(validation.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(validation, "guest_command", side_effect=execute), \
                    mock.patch.object(validation, "installed_records", side_effect=[before, after]), \
                    mock.patch.object(validation, "info_inventory", return_value={}), \
                    mock.patch.object(validation, "load_installed_receipts", side_effect=lambda names: load_receipts(names, root)), \
                    mock.patch.object(validation, "verify_receipt_files", side_effect=lambda values: verify_receipts(values, root)), \
                    mock.patch.object(validation, "source_file_inventory", side_effect=lambda values: source_inventory(values, root)), \
                    mock.patch.object(validation, "audit"), \
                    mock.patch.object(validation, "run_runtime_smoke"), \
                    mock.patch.object(validation.Path, "mkdir"), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = validation.run_guest({
                    "mode": "install", "architecture": "amd64", "lock": lock,
                    "installed_receipts": [paths["orchagent"]],
                    "installed_source_receipts": [paths["base"], paths["config-engine"]],
                })
            return result, commands, current_state

    def test_parent_source_receipts_ignore_stale_dpkg_state_but_preserve_all_sources(self):
        result, _, digest = self.run_install_with_inherited_sources()
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(result["verified_info_files"], {"var/lib/dpkg/status": digest})
        self.assertEqual(result["install"]["preserved_source_files"], 4)  # Identical Common paths overlap.
        self.assertEqual(len(result["installed_receipts"]), 1)
        self.assertEqual(len(result["installed_source_receipts"]), 2)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in result["installed_source_receipts"]))
        result, commands, _ = self.run_install_with_inherited_sources(stale_current_state=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Installed package-state file differs", result["error"])
        self.assertFalse(any(command[-2:] == ["install", "qrencode"] for command in commands))

    def test_install_rejects_inherited_file_or_link_mutations(self):
        for mutation, error in [("file", "Source payload differs"), ("link", "Source symlink differs"),
                                ("file-to-link", "Source payload is not a regular file")]:
            with self.subTest(mutation=mutation):
                result, commands, _ = self.run_install_with_inherited_sources(mutation=mutation)
                self.assertEqual(result["status"], "failed")
                self.assertIn(error, result["error"])
                self.assertTrue(any(command[-2:] == ["install", "qrencode"] for command in commands))

    def test_current_source_receipt_cannot_hide_conflicting_parent_proof(self):
        result, commands, _ = self.run_install_with_inherited_sources(conflicting_source=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Source payload differs", result["error"])
        self.assertFalse(any(command[-2:] == ["install", "qrencode"] for command in commands))

    def test_runtime_smoke_script_and_report_are_read_only_in_the_same_container(self):
        args = validation.container_arguments("sha256:example", "unique-name", Path("script"), Path("repo"), Path("sources"),
                                              Path("smoke"), Path("report"))
        mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 5)
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertTrue(any("dst=/probe/runtime_smoke.py," in mount for mount in mounts))
        self.assertTrue(any("dst=/probe/runtime_validation.json," in mount for mount in mounts))

    def test_container_cannot_write_host_inputs_or_access_network(self):
        args = validation.container_arguments("sha256:example", "unique-name", Path("script"), Path("repo"), Path("sources"))
        self.assertIn("--network=none", args)
        self.assertNotIn("--privileged", args)
        mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("--read-only", args)  # dpkg must write the disposable container's root.

    def test_wrong_native_host_fails_before_any_docker_or_download(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(validation.platform, "machine", return_value="x86_64"), \
                mock.patch.object(validation, "host_command") as command, mock.patch.object(validation, "prepare_repository") as download:
            with self.assertRaisesRegex(ValueError, "native host"):
                validation.validate(architecture="arm64", image="example", output=Path(temporary))
            command.assert_not_called()
            download.assert_not_called()
            self.assertEqual(json.loads((Path(temporary) / "receipt.json").read_text())["status"], "failed")

    def test_failed_guest_retains_logs_receipt_and_only_cleans_its_container(self):
        guest = {"status": "failed", "error": "missing ownership", "commands": []}
        stdout = "apt output\n" + validation.RESULT_PREFIX + json.dumps(guest) + "\n"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(validation.subprocess, "run") as run:
            directory = Path(temporary)
            run.side_effect = [subprocess.CompletedProcess([], 1, stdout, "dpkg warning\n"),
                               subprocess.CompletedProcess([], 0, "", "")]
            with self.assertRaisesRegex(ValueError, "probe failed"):
                validation.run_probe("sha256:image", {"mode": "install"}, directory, directory / "sources", directory, "install")
            self.assertEqual((directory / "install.stderr.log").read_text(), "dpkg warning\n")
            self.assertEqual(json.loads((directory / "install.json").read_text()), guest)
            name = run.call_args_list[0].args[0][run.call_args_list[0].args[0].index("--name") + 1]
            self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", "--volumes", name])

    def test_apt_commands_ignore_external_sources(self):
        args = validation.apt_command(["install", "qrencode"])
        self.assertIn("Dir::Etc::sourceparts=-", args)
        self.assertIn("Dir::Etc::sourcelist=/probe/sources.list", args)
        self.assertEqual(args[-2:], ["install", "qrencode"])

    def test_initial_configuration_rejects_pending_trigger_work(self):
        records = {name: {} for name in ("libc-bin", "ca-certificates", "vim-tiny", "less")}
        output = "libc-bin\tldconfig\t\n"
        with mock.patch.object(validation, "guest_command", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(ValueError, "unprocessed triggers"):
                validation.initial_configuration([], records)

    def test_initial_probe_cannot_repair_the_image_before_checking_it(self):
        request = {"mode": "initial-configuration", "architecture": "amd64"}
        with mock.patch.object(validation.platform, "machine", return_value="x86_64"), \
                mock.patch.object(validation, "guest_command", return_value=subprocess.CompletedProcess([], 0, "amd64\n", "")) as execute, \
                mock.patch.object(validation, "installed_records", return_value={"example": {}}), \
                mock.patch.object(validation, "info_inventory", return_value={}), \
                mock.patch.object(validation, "verify_receipt_files", return_value={}), \
                mock.patch.object(validation, "audit"), \
                mock.patch.object(validation, "initial_configuration", side_effect=ValueError("unconfigured package")), \
                mock.patch.object(validation.Path, "mkdir"):
            result = validation.run_guest(request)
        self.assertEqual(result["status"], "failed")
        self.assertIn("unconfigured package", result["error"])
        self.assertEqual([call.args[0] for call in execute.call_args_list], [["dpkg", "--print-architecture"]])


if __name__ == "__main__":
    unittest.main()
