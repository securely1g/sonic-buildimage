"""Fail-closed checks for module evidence collection; no build actions."""

import json
from pathlib import Path
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
