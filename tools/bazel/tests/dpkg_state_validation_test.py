"""Checks that installed-image APT evidence cannot hide incomplete package state."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import validate_dpkg_state as validation


class PackageStateValidationTest(unittest.TestCase):
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

    def test_vim_removal_cannot_leave_dangling_command_alternatives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/alternatives").mkdir(parents=True)
            (root / "usr/bin").mkdir(parents=True)
            alternative = root / "etc/alternatives/vi"
            alternative.symlink_to("../../usr/bin/vim.tiny")
            command = root / "usr/bin/vi"
            command.symlink_to("../../etc/alternatives/vi")
            with self.assertRaisesRegex(ValueError, "alternative pointing to removed"):
                validation.check_removed_alternatives("vim-tiny", {"/usr/bin/vim.tiny"}, root=root)
            alternative.unlink()
            with self.assertRaisesRegex(ValueError, "dangling command"):
                validation.check_removed_alternatives("vim-tiny", {"/usr/bin/vim.tiny"}, root=root)
            command.unlink()
            self.assertEqual(set(validation.check_removed_alternatives("vim-tiny", {"/usr/bin/vim.tiny"}, root=root).values()), {None})

    def test_less_removal_restores_more_as_pager(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/alternatives").mkdir(parents=True)
            (root / "usr/bin").mkdir(parents=True)
            (root / "bin").symlink_to("usr/bin")
            (root / "usr/bin/more").write_text("existing pager\n")
            (root / "usr/bin/pager").symlink_to("../../etc/alternatives/pager")
            pager = root / "etc/alternatives/pager"
            pager.symlink_to("../../bin/more")
            result = validation.check_removed_alternatives("less", {"/usr/bin/less"}, root=root)
            self.assertEqual(result["pager"], str((root / "usr/bin/more").resolve()))
            pager.unlink()
            with self.assertRaisesRegex(ValueError, "dangling command"):
                validation.check_removed_alternatives("less", {"/usr/bin/less"}, root=root)

    def test_full_vim_removal_restores_tiny_without_changing_its_hard_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alternatives = root / "etc/alternatives"
            alternatives.mkdir(parents=True)
            commands = root / "usr/bin"
            commands.mkdir(parents=True)
            tiny = commands / "vim.tiny"
            tiny.write_text("inherited Vim executable\n")
            os.link(tiny, commands / "vim")
            for group in ("editor", "ex", "rview", "vi", "view"):
                (commands / group).symlink_to("../../etc/alternatives/" + group)
                (alternatives / group).symlink_to("../../usr/bin/vim.tiny")
            result = validation.check_removed_alternatives("vim", {"/usr/bin/vim.basic"}, root=root)
            self.assertEqual(result["vi"], str(tiny))
            self.assertIsNone(result["vimdiff"])
            (commands / "vim").unlink()
            (commands / "vim").write_bytes(tiny.read_bytes())
            with self.assertRaisesRegex(ValueError, "vim-tiny-owned vim hard link"):
                validation.check_removed_alternatives("vim", {"/usr/bin/vim.basic"}, root=root)
            (commands / "vim").unlink()
            os.link(tiny, commands / "vim")
            (alternatives / "vi").unlink()
            (alternatives / "vi").symlink_to("../../usr/bin/vim.basic")
            with self.assertRaisesRegex(ValueError, "alternative pointing to removed"):
                validation.check_removed_alternatives("vim", {"/usr/bin/vim.basic"}, root=root)

    def test_remove_without_remaining_record_needs_no_redundant_purge(self):
        with tempfile.TemporaryDirectory() as temporary:
            former = str(Path(temporary) / "former-program")
            before = {"example": {"version": "1", "architecture": "all"}, "base": {"version": "1"}}
            after = {"base": {"version": "1"}}
            def command(args, commands, **kwargs):
                if args[:2] == ["dpkg-query", "-S"]:
                    return subprocess.CompletedProcess(args, 0, "example: " + former + "\n", "")
                if args[:2] == ["dpkg-query", "-W"]:
                    return subprocess.CompletedProcess(args, 1, "", "no packages found matching example\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            with mock.patch.object(validation, "installed_records", side_effect=[before, after]), \
                    mock.patch.object(validation, "package_paths", return_value=({former}, {})), \
                    mock.patch.object(validation, "check_removed_alternatives", return_value={}), \
                    mock.patch.object(validation, "audit"), \
                    mock.patch.object(validation, "guest_command", side_effect=command) as execute:
                result = validation.remove_and_check("example", [])
            self.assertEqual(result["removed_files"], 1)
            self.assertTrue(any("remove" in call.args[0] for call in execute.call_args_list))
            self.assertFalse(any("purge" in call.args[0] for call in execute.call_args_list))

    def test_both_native_architectures_have_two_locked_real_versions(self):
        path = Path(__file__).with_name("dpkg_transactions.lock.json")
        for architecture in ("amd64", "arm64"):
            value = validation.read_lock(path, architecture)
            self.assertEqual(value["versions"], ["2.10-3", "2.10-5"])
            self.assertTrue(all(architecture + ".deb" in entry["url"] for entry in value["packages"][architecture]))

    def test_direct_oci_upgrade_uses_real_native_socat_packages(self):
        path = Path(__file__).with_name("dpkg_transactions.lock.json")
        for architecture in ("amd64", "arm64"):
            case = validation.read_lock(path, architecture)["upgrade_packages"]["socat"]
            self.assertEqual(case["versions"], ["1.8.0.3-1+deb13u1"])
            self.assertIn(architecture + ".deb", case["packages"][architecture][0]["url"])

    def test_lock_rejects_untrusted_host_and_same_version_upgrade(self):
        source = json.loads(Path(__file__).with_name("dpkg_transactions.lock.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lock.json"
            source["packages"]["amd64"][0]["url"] = "https://example.invalid/hello.deb"
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, "official Debian"):
                validation.read_lock(path, "amd64")
            source["versions"] = ["2.10-3", "2.10-3"]
            source["packages"]["amd64"][1]["version"] = "2.10-3"
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError, "different package versions"):
                validation.read_lock(path, "amd64")

    def test_corrupt_cached_package_is_rejected_before_dpkg_or_container_execution(self):
        lock = validation.read_lock(Path(__file__).with_name("dpkg_transactions.lock.json"), "amd64")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "hello_2.10-3_amd64.deb").write_bytes(b"not a Debian package")
            with mock.patch.object(validation, "host_command") as command:
                with self.assertRaisesRegex(ValueError, "Cached Debian package differs"):
                    validation.prepare_repository(lock, "amd64", repository)
                command.assert_not_called()

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
                validation.validate(architecture="arm64", image="example", output=Path(temporary), remove_packages=["socat"])
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
                validation.run_probe("sha256:image", {"mode": "transactions"}, directory, directory / "sources", directory, "transactions")
            self.assertEqual((directory / "transactions.stderr.log").read_text(), "dpkg warning\n")
            self.assertEqual(json.loads((directory / "transactions.json").read_text()), guest)
            name = run.call_args_list[0].args[0][run.call_args_list[0].args[0].index("--name") + 1]
            self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", "--volumes", name])

    def test_apt_commands_ignore_external_sources(self):
        args = validation.apt_command(["upgrade"])
        self.assertIn("Dir::Etc::sourceparts=-", args)
        self.assertIn("Dir::Etc::sourcelist=/probe/sources.list", args)
        self.assertEqual(args[-1], "upgrade")


if __name__ == "__main__":
    unittest.main()
