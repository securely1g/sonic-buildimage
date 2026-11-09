"""Check standalone Sysmgr CI's catalog argument without running native work."""

import argparse
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import shlex
import sys
import unittest
from unittest.mock import patch

from tools.bazel.ci.source_image_worker import catalog_for
from tools.bazel.tests.source_workflow_test import shell, steps

ROOT = Path(__file__).resolve().parents[3]


class SysmgrCiWorkflowTest(unittest.TestCase):
    def test_owner_driver_receives_the_selected_review_catalog(self):
        workflow = (ROOT / ".github/workflows/bazel-sysmgr.yml").read_text()
        job = workflow.split("\njobs:\n", 1)[1]
        script = shell(steps(job)["Audit, build and validate installed packages and original tests"])
        selected = re.search(r"registry=\"\$\(python3 - <<'PYCATALOG'\n(.*?)\nPYCATALOG\n\)\"", script, re.S)
        registry = ""
        if selected:
            output = io.StringIO()
            with patch.object(Path, "cwd", return_value=ROOT), contextlib.redirect_stdout(output):
                exec(compile(selected[1], "workflow-catalog", "exec"), {})
            registry = output.getvalue().strip()
        command = script.split("python3 -B src/sonic-sysmgr/bazel/ci.py", 1)[1].split("2>&1", 1)[0]
        arguments = [value.replace("$registry", registry)
                     for value in shlex.split(command.replace("\\\n", " "))]
        spec = importlib.util.spec_from_file_location("sysmgr_ci_under_test", ROOT / "src/sonic-sysmgr/bazel/ci.py")
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)
        original = argparse.ArgumentParser.parse_args
        captured = {}

        class ArgumentsCaptured(Exception):
            pass

        def capture(parser, *args, **kwargs):
            captured["registry"] = original(parser, *args, **kwargs).registry
            raise ArgumentsCaptured

        # Stop immediately after argument parsing, before platform, Git or Bazel work.
        with patch.object(sys, "argv", ["ci.py", *arguments]), \
                patch.object(argparse.ArgumentParser, "parse_args", new=capture):
            with self.assertRaises(ArgumentsCaptured):
                ci.main()
        self.assertEqual(captured["registry"],
                         "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" +
                         catalog_for("sysmgr", ROOT))


if __name__ == "__main__":
    unittest.main()
