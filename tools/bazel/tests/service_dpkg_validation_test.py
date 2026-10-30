"""Keep service-image APT checks tied to every exact AMD64 runtime/debug archive."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import validate_service_dpkg as validation


class ServiceDpkgValidationTest(unittest.TestCase):
    def audit_build(self, program, reject=False, *, filename="runtime.tar", file_contents=""):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            artifact = output / filename
            artifact.write_bytes(b"declared TAR output")
            targets = {"probe": "//:probe"}
            paths = {"probe": artifact}
            for variant in ("runtime", "debug"):
                directory = output / variant
                directory.mkdir()
                for kind, name in (("handoff", "manifest.json"), ("payload", "payload.tar")):
                    key = variant + "_" + kind
                    targets[key] = "//:" + key
                    paths[key] = directory / name
                    paths[key].write_bytes(b"declared handoff fixture")
            graph = {
                "pathFragments": [{"id": 1, "label": filename}],
                "artifacts": [{"id": 1, "pathFragmentId": 1}],
                "actions": [{"mnemonic": "Tar", "arguments": program, "fileContents": file_contents, "outputIds": [1]}],
            }
            commands = []

            def run(arguments, **kwargs):
                commands.append(arguments)
                if arguments[1] == "aquery":
                    self.assertIn("--include_file_write_contents", arguments)
                    json.dump(graph, kwargs["stdout"])
                return SimpleNamespace(stdout="".join(
                    label + "\t" + repr([str(paths[key])]) + "\n"
                    for key, label in targets.items()))

            with mock.patch.object(validation, "TARGETS", targets), \
                    mock.patch.object(validation.subprocess, "run", side_effect=run):
                if reject:
                    with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                        validation.build_inputs(["bazel"], [], output)
                    self.assertEqual([command[1] for command in commands], ["aquery"])
                else:
                    self.assertEqual(validation.build_inputs(["bazel"], [], output), paths)
                    self.assertEqual([command[1] for command in commands], ["aquery", "build", "cquery"])
            report = json.loads((output / "action-audit.json").read_text())
            self.assertIs(report["deb_generation_authorized"], False)
            self.assertEqual(report["targets"], sorted(targets.values()))
            return report

    def test_tar_assembly_runs_after_audit(self):
        report = self.audit_build(["bsdtar", "-cf", "runtime.tar", "rootfs"])
        self.assertFalse(report["deb_packaging_actions"])
        self.assertFalse(report["packaging_wrappers"])

    def test_debian_outputs_and_commands_are_rejected_before_build(self):
        self.audit_build(["packager"], reject=True, filename="package.deb")
        self.audit_build(["dpkg-deb", "--build", "rootfs", "output"], reject=True)
        self.audit_build([], reject=True, filename="packager.sh",
                         file_contents="#!/bin/sh\ndpkg-deb --build rootfs output\n")

    def test_packaging_wrappers_are_rejected_before_build(self):
        for program in (["make", "all"], ["gmake", "all"], ["dpkg-buildpackage", "-b"]):
            with self.subTest(program=program):
                self.audit_build(program, reject=True)

    def test_cache_hits_materialize_every_runtime_and_debug_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            paths = {label: output / ("artifact-" + str(index))
                     for index, label in enumerate(validation.TARGETS.values())}
            commands = []

            def run(arguments, **kwargs):
                commands.append(arguments)
                if arguments[1] == "aquery":
                    json.dump({"actions": [{"mnemonic": "Tar", "arguments": ["bsdtar", "-cf", "payload.tar"]}]},
                              kwargs["stdout"])
                if arguments[1] == "build":
                    # A cache hit only materializes requested outputs; having a
                    # manifest does not guarantee its sibling TAR is present.
                    for label in arguments[2:]:
                        paths[label].write_bytes(b"cached output")
                return SimpleNamespace(stdout="".join(label + "\t" + repr([str(paths[label])]) + "\n"
                                                       for label in validation.TARGETS.values()))

            with mock.patch.object(validation.subprocess, "run", side_effect=run):
                resolved = validation.build_inputs(["bazel"], [], output)
            self.assertEqual([command[1] for command in commands], ["aquery", "build", "cquery"])
            for key in ("runtime_archive", "debug_archive", "orchagent_runtime_archive", "orchagent_debug_archive"):
                self.assertTrue(resolved[key].is_file(), "cache hit omitted a required image archive")
                self.assertIn(validation.TARGETS[key], commands[0][-1])
            self.assertNotIn("runtime_handoff", resolved)
            self.assertNotIn("debug_handoff", resolved)

    def run_images(self, fail_first=False):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            paths = {}
            for prefix in ("", "orchagent_"):
                for variant in ("runtime", "debug"):
                    paths[prefix + variant] = output / (prefix + variant)
                    paths[prefix + variant + "_archive"] = output / (prefix + variant + ".gz")
                    paths[prefix + variant + "_archive"].write_bytes((prefix + variant).encode())
            calls = []

            def probe(**kwargs):
                calls.append(kwargs)
                if fail_first and len(calls) == 1:
                    raise ValueError("incomplete package state")
                kwargs["output"].mkdir()
                (kwargs["output"] / "receipt.json").write_text('{"status":"passed"}\n')
                return {"status": "passed"}

            layout = SimpleNamespace(manifest={"config": {"digest": "sha256:" + "a" * 64}},
                                     descriptor={"digest": "sha256:" + "b" * 64})
            with mock.patch.object(validation, "validate_layout", return_value=layout) as inspect, \
                    mock.patch.object(validation, "validate_dpkg_state", side_effect=probe):
                if fail_first:
                    with self.assertRaisesRegex(ValueError, "transactions failed"):
                        validation.validate_dpkg_images(paths, output)
                else:
                    validation.validate_dpkg_images(paths, output)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(call.args[1] == "linux/amd64" for call in inspect.call_args_list))
                self.assertTrue(all(call["architecture"] == "amd64" for call in calls))
                self.assertEqual([call["archive"] for call in calls],
                                 [paths[name + "_archive"] for name in ("runtime", "debug", "orchagent_runtime", "orchagent_debug")])
                for call in calls:
                    self.assertEqual(call["archive_sha256"], validation.sha(call["archive"]))
                    self.assertEqual(call["config_digest"], "sha256:" + "a" * 64)
                    self.assertEqual(call["installed_source_receipts"], [
                        "/usr/share/sonic/bazel/base-packages.json",
                        "/usr/share/sonic/bazel/config-engine-packages.json",
                    ])
                    self.assertNotIn("upgrade_packages", call)
                    self.assertNotIn("remove_packages", call)
                self.assertEqual(calls[0]["installed_receipts"], ["/usr/share/sonic/bazel/syncd-packages.json"])
                self.assertEqual(calls[3]["installed_receipts"], ["/usr/share/sonic/bazel/orchagent-packages.json"])
                result = json.loads((output / "dpkg-images.json").read_text())
                self.assertEqual(result["architecture"], "amd64")
                return result

    def test_each_runtime_and_debug_archive_has_independent_transaction_checks(self):
        result = self.run_images()
        self.assertEqual({row["status"] for row in result["images"].values()}, {"passed"})

    def test_failed_first_image_does_not_hide_later_image_results(self):
        result = self.run_images(fail_first=True)
        self.assertEqual(result["images"]["syncd-runtime"]["status"], "failed")
        self.assertEqual(result["images"]["orchagent-debug"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
