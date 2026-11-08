"""Fail-closed checks for module evidence collection; no build actions."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci import resolution


GRAPH = json.dumps({"key": "<root>", "root": True, "dependencies": [
    {"key": "rules_apple@3.5.1"}, {"key": "abseil-cpp@20240722.0"},
]})
APPLE_ERRORS = """ERROR: Traceback (most recent call last):
Error: 'struct' value has no field or method 'AppleDynamicFramework'
ERROR: Error loading '@@rules_apple+//apple:apple.bzl' for module extensions, requested by https://bcr.bazel.build/modules/rules_apple/3.5.1/MODULE.bazel:27:48: at /cache/external/rules_apple+/apple/apple.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import.bzl' failed: at /cache/external/rules_apple+/apple/apple.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import.bzl' failed
"""
ABSEIL_ERRORS = "ERROR: @@bazel_tools//tools/cpp:cc_configure.bzl does not export a module extension called cc_configure_extension, yet its use is requested at https://bcr.bazel.build/modules/abseil-cpp/20240722.0/MODULE.bazel:23:29\n"


def diagnostics(*groups):
    count = len(groups)
    return "".join(groups) + f"ERROR: Results may be incomplete as {count} extension{'s' if count != 1 else ''} failed.\n"


ERRORS = diagnostics(APPLE_ERRORS, ABSEIL_ERRORS)


class GraphInspectionTest(unittest.TestCase):
    """Classify complete module graphs and narrowly recognized extension diagnostics."""

    def test_success(self):
        """Record the module count and complete inspection after a clean graph command."""
        receipt = resolution.inspect_graph(GRAPH, "", 0)
        self.assertTrue(receipt["extension_inspection_complete"])
        self.assertEqual(receipt["module_count"], 3)

    def test_known_unused_extensions_are_recorded(self):
        """Retain known failures without calling their extension inspection complete."""
        for groups in ((APPLE_ERRORS,), (ABSEIL_ERRORS,), (APPLE_ERRORS, ABSEIL_ERRORS)):
            with self.subTest(groups=groups):
                receipt = resolution.inspect_graph(GRAPH, diagnostics(*groups), 2)
                self.assertTrue(receipt["module_graph_complete"])
                self.assertFalse(receipt["extension_inspection_complete"])
                self.assertEqual(len(receipt["unused_extension_failures"]), len(groups))

    def test_unaffected_module_may_use_another_version(self):
        """Do not constrain the version of a module that emitted no failure."""
        for graph, errors in ((GRAPH.replace("20240722.0", "20250127.1"), APPLE_ERRORS),
                              (GRAPH.replace("3.5.1", "4.0.0"), ABSEIL_ERRORS)):
            with self.subTest(graph=graph):
                receipt = resolution.inspect_graph(graph, diagnostics(errors), 2)
                self.assertEqual(len(receipt["unused_extension_failures"]), 1)

    def test_failing_module_version_must_match_graph(self):
        """Reject known diagnostics when their module versions disagree with the graph."""
        for graph in (GRAPH.replace("20240722.0", "20250127.1"), GRAPH.replace("3.5.1", "4.0.0")):
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                resolution.inspect_graph(graph, ERRORS, 2)

    def test_partial_and_duplicate_groups_rejected(self):
        """Require each recognized diagnostic group to be complete and appear only once."""
        apple_lines = APPLE_ERRORS.splitlines(keepends=True)
        for index in range(len(apple_lines)):
            partial = "".join(line for position, line in enumerate(apple_lines) if position != index)
            for errors in (diagnostics(partial), diagnostics(partial, ABSEIL_ERRORS)):
                with self.subTest(errors=errors), self.assertRaises(ValueError):
                    resolution.inspect_graph(GRAPH, errors, 2)
        for extra in (*apple_lines, ABSEIL_ERRORS, APPLE_ERRORS):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                resolution.inspect_graph(GRAPH, extra + ERRORS, 2)

    def test_summary_must_match_complete_group_count(self):
        """Reject missing, repeated or malformed summaries and incorrect failure counts."""
        cases = (
            ERRORS.replace("2 extensions", "1 extension"),
            diagnostics(APPLE_ERRORS).replace("1 extension", "2 extensions"),
            diagnostics(ABSEIL_ERRORS).replace("1 extension", "1 extensions"),
            ERRORS + "ERROR: Results may be incomplete as 2 extensions failed.\n",
            "".join(ERRORS.splitlines(keepends=True)[:-1]),
        )
        for errors in cases:
            with self.subTest(errors=errors), self.assertRaises(ValueError):
                resolution.inspect_graph(GRAPH, errors, 2)

    def test_other_failures_rejected(self):
        """Keep unknown messages, changed signatures and unexpected exit codes fatal."""
        for diagnostics, code in ((ERRORS + "ERROR: download failed\n", 2),
                                  (ERRORS.replace("AppleDynamicFramework", "another_symbol"), 2),
                                  (ERRORS.replace("20240722.0", "20250101.0"), 2),
                                  (ERRORS, 1), ("", 2)):
            with self.subTest(diagnostics=diagnostics, code=code), self.assertRaises(ValueError):
                resolution.inspect_graph(GRAPH, diagnostics, code)

    def test_empty_truncated_or_incomplete_graph_rejected(self):
        """Reject invalid graph data and modules whose only occurrence is unexpanded."""
        incomplete = json.dumps({"key": "<root>", "root": True, "dependencies": [
            {"key": "missing@1", "unexpanded": True},
        ]})
        for graph in ("", "{", "{}", incomplete):
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                resolution.inspect_graph(graph, ERRORS, 2)

    def test_repeated_reference_requires_expanded_module(self):
        """Count an unexpanded repeat once when its module already has an expanded entry."""
        graph = json.loads(GRAPH)
        graph["dependencies"].append({"key": "rules_apple@3.5.1", "unexpanded": True})
        self.assertEqual(resolution.inspect_graph(json.dumps(graph), ERRORS, 2)["module_count"], 3)


class CollectionDiagnosticsTest(unittest.TestCase):
    """Preserve the real subprocess error when graph output cannot be inspected."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = self.root / "MODULE.bazel.lock"
        self.lock.write_text('{"registryFileHashes": {}}\n')
        self.output = self.root / "evidence"
        self.output.mkdir()

    def command(self, stdout, stderr, returncode):
        script = self.root / "graph.py"
        script.write_text("import sys\n" +
                          "sys.stdout.write(" + repr(stdout) + ")\n" +
                          "sys.stderr.write(" + repr(stderr) + ")\n" +
                          "sys.exit(" + str(returncode) + ")\n")
        return (sys.executable, str(script))

    def test_empty_failed_graph_reports_primary_error_before_inspection(self):
        """An empty graph must expose its download error and exit code, not just JSONDecodeError."""
        primary = "ERROR: repository download failed: HTTP 504\n"
        console = io.StringIO()
        inspect = resolution.inspect_graph

        def check_before_parse(*args):
            self.assertIn("Bazel mod graph returned 17", console.getvalue())
            self.assertIn(primary, console.getvalue())
            return inspect(*args)

        before = self.lock.read_bytes()
        with contextlib.redirect_stderr(console), \
                mock.patch.object(resolution, "inspect_graph", side_effect=check_before_parse), \
                self.assertRaisesRegex(ValueError, "not valid JSON.*return code 17"):
            resolution.collect(self.root, self.output, bazel=self.command("", primary, 17))
        self.assertEqual((self.output / "module-graph.stderr").read_text(), primary)
        self.assertEqual((self.output / "module-graph.json").read_text(), "")
        receipt = json.loads((self.output / "module-graph-inspection.json").read_text())
        self.assertEqual(receipt["returncode"], 17)
        self.assertEqual(self.lock.read_bytes(), before)

    def test_large_diagnostics_are_bounded_but_complete_local_evidence_survives(self):
        """Keep the causal beginning/end while avoiding huge stdout/stderr dumps in job logs."""
        stderr = "first error\n" + "x" * 100000 + "\nlast error\n"
        stdout = "not-json-start\n" + "y" * 100000 + "\nnot-json-end\n"
        console = io.StringIO()
        with contextlib.redirect_stderr(console), self.assertRaisesRegex(ValueError, "return code 33"):
            resolution.collect(self.root, self.output, bazel=self.command(stdout, stderr, 33))
        text = console.getvalue()
        self.assertLess(len(text.encode()), 11000)
        for marker in ("first error", "last error", "not-json-start", "not-json-end", "bytes omitted"):
            self.assertIn(marker, text)
        self.assertEqual((self.output / "module-graph.stderr").read_text(), stderr)
        self.assertEqual((self.output / "module-graph.json").read_text(), stdout)

    def test_zero_exit_with_invalid_json_is_still_rejected_with_diagnostics(self):
        """A process success must not turn malformed graph output into accepted evidence."""
        console = io.StringIO()
        with contextlib.redirect_stderr(console), self.assertRaisesRegex(ValueError, "return code 0"):
            resolution.collect(self.root, self.output,
                               bazel=self.command("not JSON", "unexpected wrapper output\n", 0))
        self.assertIn("unexpected wrapper output", console.getvalue())
        self.assertIn("not JSON", console.getvalue())

    def test_known_unused_extension_failure_keeps_existing_acceptance(self):
        """Reporting a nonzero process result must not change the existing complete-graph policy."""
        console = io.StringIO()
        with contextlib.redirect_stderr(console), mock.patch.object(resolution, "retain", return_value={}):
            result = resolution.collect(self.root, self.output, bazel=self.command(GRAPH, ERRORS, 2))
        self.assertTrue(result["inspection"]["module_graph_complete"])
        self.assertFalse(result["inspection"]["extension_inspection_complete"])
        self.assertEqual(len(result["inspection"]["unused_extension_failures"]), 2)


class SourceScopeTest(unittest.TestCase):
    """Exercise post-Make provenance checks against real Git submodule state."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.root = directory / "workspace"
        component = directory / "component"
        for repository in (self.root, component):
            repository.mkdir()
            self.git(repository, "init", "--quiet")
            self.git(repository, "config", "user.name", "SONiC test")
            self.git(repository, "config", "user.email", "securely1g@gmail.com")
        (component / "source.cc").write_text("original producer source\n")
        self.git(component, "add", "source.cc")
        self.git(component, "commit", "--quiet", "-m", "Seed producer fixture")
        (self.root / "BUILD.bazel").write_text("# Declared container build input\n")
        (self.root / ".gitignore").write_text("MODULE.bazel.lock\n")
        self.git(self.root, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet",
                 str(component), "deps/component")
        self.component = self.root / "deps/component"
        self.git(self.component, "config", "user.name", "SONiC test")
        self.git(self.component, "config", "user.email", "securely1g@gmail.com")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "--quiet", "-m", "Pin producer fixture")
        self.lock = self.root / "MODULE.bazel.lock"
        self.lock.write_text(json.dumps({"registryFileHashes": {}}) + "\n")
        self.output = directory / "evidence"
        self.output.mkdir()

    @staticmethod
    def git(repository, *args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True,
                              capture_output=True, text=True).stdout

    def change_component(self, *, commit=False):
        (self.component / "source.cc").write_text("patched by the legacy Make producer\n")
        if commit:
            self.git(self.component, "commit", "--quiet", "-am", "Apply producer fixture patch")

    def test_default_rejects_unrelated_submodule_dirt_and_commit(self):
        """Cold source jobs retain their strict whole-checkout cleanliness check."""
        for commit in (False, True):
            with self.subTest(commit=commit):
                self.change_component(commit=commit)
                with self.assertRaises(subprocess.CalledProcessError):
                    resolution.retain(self.root, self.output, GRAPH)
                self.assertFalse((self.output / "MODULE.bazel.lock").exists())

    def test_scoped_inputs_allow_unrelated_make_submodule_changes(self):
        """An FRR-style patched commit must not invalidate unchanged OCI inputs."""
        for commit in (False, True):
            with self.subTest(commit=commit):
                self.change_component(commit=commit)
                result = resolution.retain(self.root, self.output, GRAPH,
                                           source_files=["BUILD.bazel"])
                self.assertEqual(result["checked_source_files"], ["BUILD.bazel"])
                self.assertTrue(result["tracked_files_unchanged"])
                self.assertEqual((self.output / "MODULE.bazel.lock").read_bytes(), self.lock.read_bytes())

    def test_scoped_inputs_reject_changes_to_checked_root_file(self):
        """Post-Make mode still rejects altered build declarations against HEAD."""
        (self.root / "BUILD.bazel").write_text("# Changed source target\n")
        with self.assertRaises(subprocess.CalledProcessError):
            resolution.retain(self.root, self.output, GRAPH, source_files=["BUILD.bazel"])
        self.assertFalse((self.output / "MODULE.bazel.lock").exists())

    def test_scoped_submodule_input_checks_its_gitlink(self):
        """A file inside a producer submodule also pins that producer's HEAD."""
        result = resolution.retain(self.root, self.output, GRAPH,
                                   source_files=["deps/component/source.cc"])
        self.assertEqual(result["checked_source_files"],
                         ["deps/component", "deps/component/source.cc"])
        self.change_component(commit=True)
        # A checkout's local ignore policy must not hide a changed producer pin.
        self.git(self.root, "config", "submodule.deps/component.ignore", "all")
        with self.assertRaises(subprocess.CalledProcessError):
            resolution.retain(self.root, self.output, GRAPH,
                              source_files=["deps/component/source.cc"])

    def test_invalid_or_escaping_scope_is_rejected(self):
        """An empty, unsafe or Git-magic scope cannot bypass source validation."""
        (self.root / "escape").symlink_to(Path(self.temporary.name) / "component", target_is_directory=True)
        for files in ([], (), "BUILD.bazel", [""], ["."], ["../component/source.cc"],
                      ["/etc/passwd"], ["deps/../BUILD.bazel"], ["BUILD.bazel\n"],
                      [":(exclude)BUILD.bazel"], ["*.bazel"], ["escape/source.cc"]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                resolution.retain(self.root, self.output, GRAPH, source_files=files)

    def test_scoped_inputs_still_reject_a_tracked_lock(self):
        """Source scope never relaxes the generated module-lock contract."""
        self.git(self.root, "add", "--force", "MODULE.bazel.lock")
        with self.assertRaisesRegex(ValueError, "generated, not tracked"):
            resolution.retain(self.root, self.output, GRAPH, source_files=["BUILD.bazel"])

    def test_collect_forwards_scope_without_changing_generated_lock(self):
        """The graph collector uses the same scoped guard and preserves the tested lock."""
        self.change_component(commit=True)
        before = self.lock.read_bytes()
        graph_command = Path(self.temporary.name) / "graph.py"
        graph_command.write_text("print(" + repr(GRAPH) + ")\n")
        result = resolution.collect(self.root, self.output,
                                    bazel=(sys.executable, str(graph_command)),
                                    source_files=["BUILD.bazel"])
        self.assertEqual(result["checked_source_files"], ["BUILD.bazel"])
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertTrue(result["inspection"]["module_graph_complete"])


if __name__ == "__main__":
    unittest.main()
