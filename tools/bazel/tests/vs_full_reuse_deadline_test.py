"""Check complete assembly/cleanup time before entering the root supervisor."""
import argparse
import ast
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[3] / "tools/bazel/ci/vs_runtime_check.py"


def namespace():
    functions = {"require", "full_vs_deadline", "main"}
    constants = {"FULL_VS_BODY_SECONDS", "ROOT_CLEANUP_SECONDS"}
    body = []
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "ProbeFailure":
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            body.append(node)
    values = {name: globals()[name] for name in
              ("argparse", "hashlib", "json", "os", "Path", "sys", "time")}
    values.update(private_daemon=mock.Mock(), run_full_vs=mock.Mock(return_value={"status": "passed"}))
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), values)
    return values


class FullVsDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.n = namespace()
        self.required = self.n["FULL_VS_BODY_SECONDS"] + self.n["ROOT_CLEANUP_SECONDS"] + 30
        self.clock = mock.patch.object(time, "time", return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_accepts_complete_body_and_cleanup_window(self):
        self.n["full_vs_deadline"](1000 + self.required)

    def test_rejects_missing_or_noninteger_deadlines(self):
        for value in (None, True, 9000.0, "9000", 0, -1):
            with self.subTest(value=value), self.assertRaises(self.n["ProbeFailure"]):
                self.n["full_vs_deadline"](value)

    def test_rejects_expired_deadline(self):
        with self.assertRaises(self.n["ProbeFailure"]):
            self.n["full_vs_deadline"](999)

    def test_does_not_spend_the_cleanup_reserve(self):
        with self.assertRaises(self.n["ProbeFailure"]):
            self.n["full_vs_deadline"](1000 + self.required - 1)

    def test_root_entrypoint_rechecks_time_before_any_daemon(self):
        argv = ["runtime", "--full-vs-root", "--worker", "fixture", "--run", "1",
                "--attempt", "1", "--temporary", "/unused",
                "--full-vs-context", "/unused/context.json", "--full-vs-context-sha256", "a" * 64,
                "--full-vs-job-deadline", str(1000 + self.required - 1)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(os, "umask"), \
                self.assertRaises(self.n["ProbeFailure"]):
            self.n["main"]()
        self.n["private_daemon"].assert_not_called()
        self.n["run_full_vs"].assert_not_called()

    def test_parent_entrypoint_passes_the_same_deadline_to_supervision(self):
        deadline = 1000 + self.required + 1
        argv = ["runtime", "--full-vs", "--worker", "fixture", "--output", "/unused/result.json",
                "--full-vs-context", "/unused/context.json", "--full-vs-context-sha256", "a" * 64,
                "--full-vs-job-deadline", str(deadline)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(os, "umask"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.n["main"](), 0)
        self.assertEqual(self.n["run_full_vs"].call_args.args[-1], deadline)
        self.n["private_daemon"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
