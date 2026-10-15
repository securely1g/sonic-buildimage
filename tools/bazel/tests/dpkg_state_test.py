"""Check installed ownership and lifecycle metadata without producing DEBs."""

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import dpkg_state
from tools.bazel.oci.oci_layout import OciLayout


def archive(path, files):
    with tarfile.open(path, "w") as output:
        for name, value in files.items():
            content, mode = value if isinstance(value, tuple) else (value, 0o644)
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                output.addfile(info)
            else:
                if isinstance(content, str):
                    content = content.encode()
                info.size, info.mode = len(content), mode
                output.addfile(info, io.BytesIO(content))
    return path


def control(name="example", version="1.0", architecture="amd64", extra=""):
    return (f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
            "Maintainer: Example <example@example.com>\nDescription: Example package\n" + extra)


class DpkgStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base_status = control("inherited") + "Status: install ok installed\n\n"
        self.base_files = {"var/lib/dpkg/status": self.base_status,
                           "var/lib/dpkg/info/inherited.list": "/.\n/usr/bin/inherited\n",
                           "usr/bin/inherited": "inherited"}
        self.status = self.root / "status"
        self.status.write_text(self.base_status + control() + "Status: install ok installed\n\n")
        self.payload = archive(self.root / "payload.tar", {
            "usr/bin/example": ("program", 0o755), "etc/example.conf": "setting=original\n",
        })
        self.controls = archive(self.root / "control.tar", {
            "control": control(), "conffiles": "/etc/example.conf\n",
            "postinst": ("#!/bin/sh\nexit 0\n", 0o755),
            "postrm": ("#!/bin/sh\nexit 0\n", 0o755),
        })
        self.selection = self.root / "selection.json"
        self.selection.write_text(json.dumps({"base_manifest_digest": "sha256:base", "selected": []}))
        self.inputs = {"architecture": "amd64", "sources": [{
            "package": "example", "payload": str(self.payload), "control": str(self.controls),
        }], "receipt_path": "usr/share/sonic/bazel/example-dpkg-state.json"}
        self.output = self.root / "output"
        self.mtree = self.root / "metadata.mtree"
        self.receipt = self.root / "receipt.json"

    def run_build(self):
        base = archive(self.root / "base.tar", self.base_files)
        layout = OciLayout({"digest": "sha256:base"}, {}, {}, (base,))
        with patch.object(dpkg_state, "validate_layout", return_value=layout):
            return dpkg_state.build(self.root / "base", self.status, self.selection, self.inputs,
                                    self.output, self.mtree, self.receipt)

    def text(self, name):
        return (self.output / name).read_text()

    def test_package_ownership_and_configuration_are_queryable_by_dpkg(self):
        receipt = self.run_build()
        self.assertEqual(self.text("var/lib/dpkg/info/example.list"),
                         "/.\n/etc\n/etc/example.conf\n/usr\n/usr/bin\n/usr/bin/example\n")
        self.assertEqual(self.text("var/lib/dpkg/info/example.md5sums"),
                         hashlib.md5(b"program").hexdigest() + "  usr/bin/example\n")
        status = dpkg_state.records(self.text("var/lib/dpkg/status"))
        self.assertIn(hashlib.md5(b"setting=original\n").hexdigest(), status["example"]["Conffiles"])
        self.assertFalse(receipt["maintainer_scripts_executed"])
        self.assertFalse(receipt["installation_triggers_processed"])
        self.assertEqual(json.loads(self.text(self.inputs["receipt_path"])), receipt)
        self.assertEqual(receipt["info_files"]["var/lib/dpkg/info/example.postinst"]["mode"], 0o755)
        self.assertEqual(receipt["info_files"]["var/lib/dpkg/status"], {
            "sha256": hashlib.sha256((self.output / "var/lib/dpkg/status").read_bytes()).hexdigest(),
            "mode": 0o644,
        })
        if shutil.which("dpkg-query"):
            result = subprocess.run(["dpkg-query", "--admindir=" + str(self.output / "var/lib/dpkg"),
                                     "--listfiles", "example"], check=True, text=True, capture_output=True)
            self.assertIn("/usr/bin/example\n", result.stdout)

    def test_replacement_deletes_stale_info_and_obsolete_owned_files_only(self):
        old = control(version="0.9") + "Status: install ok installed\n\n"
        self.base_files.update({"var/lib/dpkg/status": self.base_status + old,
            "var/lib/dpkg/info/example.list": "/usr/bin/old-example\n/usr/bin/shared\n/usr/bin/example\n",
            "var/lib/dpkg/info/example.preinst": ("obsolete", 0o755),
            "var/lib/dpkg/info/inherited.list": "/usr/bin/inherited\n/usr/bin/shared\n",
            "usr/bin/old-example": "old", "usr/bin/shared": "shared", "usr/bin/example": "old"})
        receipt = self.run_build()
        self.assertEqual(receipt["removed_paths"], ["usr/bin/old-example", "var/lib/dpkg/info/example.preinst"])
        self.assertNotIn("var/lib/dpkg/info/.wh.example.preinst", receipt["info_files"])
        self.assertTrue((self.output / "usr/bin/.wh.old-example").is_file())
        self.assertFalse((self.output / "var/lib/dpkg/info/inherited.list").exists())

    def test_preserves_removed_conffile_for_later_purge(self):
        old = control(version="0.9") + "Status: install ok installed\nConffiles:\n /etc/old.conf abcdef obsolete\n\n"
        self.base_files.update({"var/lib/dpkg/status": self.base_status + old,
            "var/lib/dpkg/info/example.list": "/etc/old.conf\n", "etc/old.conf": "changed by user"})
        receipt = self.run_build()
        self.assertIn(" /etc/old.conf abcdef obsolete", self.text("var/lib/dpkg/status"))
        self.assertIn("/etc/old.conf\n", self.text("var/lib/dpkg/info/example.list"))
        self.assertNotIn("etc/old.conf", receipt["removed_paths"])

    def test_remove_on_upgrade_conffile_is_safe_only_when_already_absent(self):
        self.controls = archive(self.controls, {"control": control(), "conffiles":
            "remove-on-upgrade /etc/old.conf\n/etc/example.conf\n"})
        self.run_build()
        self.assertEqual(self.text("var/lib/dpkg/info/example.conffiles"),
                         "remove-on-upgrade /etc/old.conf\n/etc/example.conf\n")
        self.assertNotIn("/etc/old.conf", self.text("var/lib/dpkg/status"))
        self.base_files["etc/old.conf"] = "user configuration"
        with self.assertRaisesRegex(ValueError, "needs a real upgrade"):
            self.run_build()

    def test_fresh_install_conffile_can_move_to_another_selected_package(self):
        self.controls = archive(self.controls, {"control": control(), "conffiles":
            "remove-on-upgrade /etc/old.conf\n/etc/example.conf\n"})
        replacement_payload = archive(self.root / "replacement.tar", {"etc/old.conf": "new owner default"})
        replacement_control = archive(self.root / "replacement.control.tar", {
            "control": control("replacement"), "conffiles": "/etc/old.conf\n"})
        self.inputs["sources"].append({"package": "replacement", "payload": str(replacement_payload),
                                       "control": str(replacement_control)})
        self.inputs["payload_layers"] = [str(self.payload), str(replacement_payload)]
        self.status.write_text(self.status.read_text() + control("replacement") + "Status: install ok installed\n\n")
        self.run_build()
        self.assertNotIn("/etc/old.conf", self.text("var/lib/dpkg/info/example.list"))
        self.assertIn("/etc/old.conf\n", self.text("var/lib/dpkg/info/replacement.list"))

    def test_preserves_config_files_records_missing_from_status_only_helper(self):
        self.base_files["var/lib/dpkg/status"] += control("removed") + "Status: deinstall ok config-files\n\n"
        self.run_build()
        self.assertIn("removed", dpkg_state.records(self.text("var/lib/dpkg/status")))

    def test_native_multiarch_info_filename(self):
        self.inputs["architecture"] = "arm64"
        self.status.write_text(self.base_status + control(architecture="arm64", extra="Multi-Arch: same\n") + "Status: install ok installed\n\n")
        self.run_build()
        self.assertTrue((self.output / "var/lib/dpkg/info/example:arm64.list").exists())
        self.assertFalse((self.output / "var/lib/dpkg/info/example.list").exists())

    def test_imported_deb_metadata_cannot_be_relabelled_for_another_architecture(self):
        self.inputs["architecture"] = "arm64"
        self.status.write_text(self.base_status + control(architecture="arm64") + "Status: install ok installed\n\n")
        self.inputs["imported"] = self.inputs.pop("sources")
        with self.assertRaisesRegex(ValueError, "imported package architecture mismatch"):
            self.run_build()

    def test_only_selected_apt_candidates_are_installed_and_hash_checked(self):
        self.inputs.pop("sources")
        self.inputs["apt"] = [{"payload": str(self.payload), "control": str(self.controls)}]
        self.selection.write_text(json.dumps({"base_manifest_digest": "sha256:base", "selected": [{
            "package": "example", "control_sha256": dpkg_state.sha(self.controls),
            "payload_sha256": dpkg_state.sha(self.payload),
        }]}))
        self.run_build()
        self.assertTrue((self.output / "var/lib/dpkg/info/example.list").exists())
        self.payload.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "selected APT bytes changed"):
            self.run_build()

    def test_actual_payload_layer_filters_files_and_preserves_pristine_conffile_hash(self):
        self.payload = archive(self.payload, {"usr/bin/example": "program", "usr/share/doc/example/readme": "doc",
                                              "etc/example.conf": "original"})
        actual = archive(self.root / "filtered.tar", {"usr/bin/example": "program", "etc/example.conf": "image-specific override"})
        self.inputs["payload_layers"] = [str(actual)]
        self.run_build()
        self.assertNotIn("readme", self.text("var/lib/dpkg/info/example.list"))
        self.assertIn(hashlib.md5(b"original").hexdigest(), self.text("var/lib/dpkg/status"))

    def test_declared_image_command_is_owned_by_its_package(self):
        actual = archive(self.root / "command.tar", {"usr/bin/extra-example": ("program", 0o755)})
        self.inputs["payload_layers"] = [str(self.payload), str(actual)]
        self.inputs["extra_owned_paths"] = {"example": ["/usr/bin/extra-example"]}
        self.run_build()
        self.assertIn("/usr/bin/extra-example\n", self.text("var/lib/dpkg/info/example.list"))
        self.assertIn(hashlib.md5(b"program").hexdigest() + "  usr/bin/extra-example\n",
                      self.text("var/lib/dpkg/info/example.md5sums"))
        self.base_files["var/lib/dpkg/info/inherited.list"] += "/usr/bin/extra-example\n"
        with self.assertRaisesRegex(ValueError, "belongs to an inherited package"):
            self.run_build()

    def test_declared_image_command_must_exist(self):
        self.inputs["payload_layers"] = [str(self.payload)]
        self.inputs["extra_owned_paths"] = {"example": ["usr/bin/missing"]}
        with self.assertRaisesRegex(ValueError, "absent from installed image layers"):
            self.run_build()

    def test_trigger_interest_registration_retains_unrelated_owners(self):
        self.controls = archive(self.controls, {"control": control(), "triggers":
            "interest-noawait /usr/share/example\ninterest-await example-change\nactivate-noawait ldconfig\n"})
        self.base_files.update({"var/lib/dpkg/triggers/File": "/usr/share/old example/noawait\n/usr/share/other inherited\n",
                               "var/lib/dpkg/triggers/example-change": "inherited\n",
                               "var/lib/dpkg/triggers/ldconfig": "libc-bin\n"})
        receipt = self.run_build()
        self.assertEqual(self.text("var/lib/dpkg/triggers/File"),
                         "/usr/share/example example/noawait\n/usr/share/other inherited\n")
        self.assertEqual(self.text("var/lib/dpkg/triggers/example-change"), "example\ninherited\n")
        self.assertEqual(receipt["packages"][0]["installation_trigger_activations"], ["ldconfig"])

    def test_unknown_trigger_directive_fails_closed(self):
        archive(self.controls, {"control": control(), "triggers": "unknown-action something\n"})
        with self.assertRaisesRegex(ValueError, "unsupported trigger"):
            self.run_build()

    def test_missing_source_conffile_fails_closed(self):
        archive(self.payload, {"usr/bin/example": "program"})
        with self.assertRaisesRegex(ValueError, "conffile is absent"):
            self.run_build()

    def test_controls_fill_missing_relationships_but_reject_conflicting_depends(self):
        archive(self.controls, {"control": control(extra="Conflicts: older-example\nDepends: inherited\n")})
        self.run_build()
        self.assertIn("Conflicts: older-example", self.text("var/lib/dpkg/status"))
        self.status.write_text(self.status.read_text().replace("Package: example\n", "Package: example\nDepends: other\n"))
        with self.assertRaisesRegex(ValueError, "relationship differs"):
            self.run_build()

    def test_source_control_manifest_hash_is_checked_before_using_scripts(self):
        controls = self.root / "reviewed"
        controls.mkdir()
        data = {"control": control(), "postinst": "#!/bin/sh\nexit 0\n"}
        entries = {}
        for name, contents in data.items():
            (controls / name).write_text(contents)
            entries[name] = {"path": name, "sha256": dpkg_state.digest(contents.encode()),
                             "mode": 0o755 if name == "postinst" else 0o644}
        manifest = controls / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "packages": {"example": {"version": "1.0", "control_files": entries}}}))
        self.inputs["sources"][0].pop("control")
        self.inputs["source_controls"] = str(manifest)
        (controls / "postinst").write_text("changed")
        with self.assertRaisesRegex(ValueError, "reviewed bytes"):
            self.run_build()

    @unittest.skipUnless(shutil.which("dpkg"), "requires host dpkg for an isolated administrative directory")
    def test_remove_preserves_aliased_conffile_and_purge_removes_it(self):
        archive(self.payload, {"usr/bin/example": "program", "run/example.conf": "configuration"})
        archive(self.controls, {"control": control(), "conffiles": "/var/run/example.conf\n"})
        self.run_build()
        listing = self.text("var/lib/dpkg/info/example.list")
        self.assertIn("/var/run/example.conf\n", listing)
        self.assertNotIn("\n/run/example.conf\n", listing)
        for directory in ("run", "var", "usr/bin"):
            (self.output / directory).mkdir(parents=True, exist_ok=True)
        (self.output / "var/run").symlink_to("../run")
        configuration = self.output / "run/example.conf"
        configuration.write_text("user changes")
        (self.output / "usr/bin/example").write_text("program")
        command = ["dpkg", "--root=" + str(self.output), "--log=" + str(self.root / "dpkg.log"), "--force-not-root"]
        subprocess.run(command + ["--remove", "example"], check=True, capture_output=True)
        self.assertEqual(configuration.read_text(), "user changes")
        subprocess.run(command + ["--purge", "example"], check=True, capture_output=True)
        self.assertFalse(configuration.exists())

    def test_source_replaces_selected_apt_and_retains_ownership_of_remaining_files(self):
        apt_payload = archive(self.root / "apt-payload.tar", {
            "usr/bin/example": "old binary", "etc/example.conf": "original configuration", "usr/share/doc/example/copyright": "license"})
        apt_control = archive(self.root / "apt-control.tar", {"control": control(version="0.9")})
        self.inputs["apt"] = [{"payload": str(apt_payload), "control": str(apt_control)}]
        self.selection.write_text(json.dumps({"base_manifest_digest": "sha256:base", "selected": [{
            "package": "example", "control_sha256": dpkg_state.sha(apt_control), "payload_sha256": dpkg_state.sha(apt_payload),
        }]}))
        self.inputs["payload_layers"] = [str(apt_payload), str(self.payload)]
        receipt = self.run_build()
        listing = self.text("var/lib/dpkg/info/example.list")
        self.assertIn("/usr/share/doc/example/copyright", listing)
        self.assertIn(hashlib.md5(b"program").hexdigest(), self.text("var/lib/dpkg/info/example.md5sums"))
        self.assertEqual(receipt["packages"][0]["provenance"]["replaced_selected_apt"]["version"], "0.9")

    def test_ordered_layer_whiteout_removes_package_ownership(self):
        archive(self.payload, {"usr/bin/example": "program", "usr/bin/removed": "obsolete", "etc/example.conf": "original"})
        deletion = archive(self.root / "delete.tar", {"usr/bin/.wh.removed": b""})
        self.inputs["payload_layers"] = [str(self.payload), str(deletion)]
        self.run_build()
        self.assertNotIn("/usr/bin/removed", self.text("var/lib/dpkg/info/example.list"))

    def test_duplicate_directory_members_are_normal_archive_input(self):
        with tarfile.open(self.payload, "a") as output:
            directory = tarfile.TarInfo("usr")
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            output.addfile(directory)
            output.addfile(directory)
        self.run_build()
        self.assertEqual(self.text("var/lib/dpkg/info/example.list").splitlines().count("/usr"), 1)

    @unittest.skipUnless(shutil.which("dpkg-trigger") and shutil.which("dpkg-query"), "requires native dpkg utilities")
    def test_dpkg_recognizes_generated_trigger_registration(self):
        archive(self.controls, {"control": control(), "triggers": "interest-noawait example-change\n"})
        self.run_build()
        administrative = "--admindir=" + str(self.output / "var/lib/dpkg")
        subprocess.run(["dpkg-trigger", administrative, "--by-package=inherited", "--no-await", "example-change"],
                       check=True, capture_output=True)
        result = subprocess.run(["dpkg-query", administrative, "-W", "-f=${Triggers-Pending}\n", "example"],
                                check=True, text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "example-change")


if __name__ == "__main__":
    unittest.main()
