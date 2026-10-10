"""Fail-closed checks for module evidence collection; no build actions."""

import json
import unittest

import resolution


GRAPH = json.dumps({"key": "<root>", "root": True, "dependencies": [
    {"key": "rules_apple@3.5.1"}, {"key": "abseil-cpp@20240722.0"},
]})
ERRORS = """ERROR: Traceback (most recent call last):
Error: 'struct' value has no field or method 'AppleDynamicFramework'
ERROR: @@bazel_tools//tools/cpp:cc_configure.bzl does not export a module extension called cc_configure_extension, yet its use is requested at https://bcr.bazel.build/modules/abseil-cpp/20240722.0/MODULE.bazel:23:29
ERROR: Error loading '@@rules_apple+//apple:apple.bzl' for module extensions, requested by https://bcr.bazel.build/modules/rules_apple/3.5.1/MODULE.bazel:27:48: at /cache/external/rules_apple+/apple/apple.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import.bzl' failed: at /cache/external/rules_apple+/apple/apple.bzl:27:5: initialization of module 'apple/internal/apple_xcframework_import.bzl' failed
ERROR: Results may be incomplete as 2 extensions failed.
"""


class GraphInspectionTest(unittest.TestCase):
    def test_success(self):
        receipt = resolution.inspect_graph(GRAPH, "", 0)
        self.assertTrue(receipt["extension_inspection_complete"])
        self.assertEqual(receipt["module_count"], 3)

    def test_known_unused_extensions_are_recorded(self):
        receipt = resolution.inspect_graph(GRAPH, ERRORS, 2)
        self.assertTrue(receipt["module_graph_complete"])
        self.assertFalse(receipt["extension_inspection_complete"])
        self.assertEqual(len(receipt["unused_extension_failures"]), 2)

    def test_other_failures_rejected(self):
        for diagnostics, code in ((ERRORS + "ERROR: download failed\n", 2),
                                  (ERRORS.replace("AppleDynamicFramework", "another_symbol"), 2),
                                  (ERRORS.replace("20240722.0", "20250101.0"), 2),
                                  (ERRORS, 1), ("", 2)):
            with self.subTest(diagnostics=diagnostics, code=code), self.assertRaises(ValueError):
                resolution.inspect_graph(GRAPH, diagnostics, code)

    def test_empty_truncated_or_incomplete_graph_rejected(self):
        incomplete = json.dumps({"key": "<root>", "root": True, "dependencies": [
            {"key": "missing@1", "unexpanded": True},
        ]})
        for graph in ("", "{", "{}", incomplete):
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                resolution.inspect_graph(graph, ERRORS, 2)

    def test_repeated_reference_requires_expanded_module(self):
        graph = json.loads(GRAPH)
        graph["dependencies"].append({"key": "rules_apple@3.5.1", "unexpanded": True})
        self.assertEqual(resolution.inspect_graph(json.dumps(graph), ERRORS, 2)["module_count"], 3)


if __name__ == "__main__":
    unittest.main()
