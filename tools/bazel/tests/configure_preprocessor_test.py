"""Exercise the new shell transport without fetching external repositories.

The tests execute the helper and private-vector reader added by the patch.
The native BMv2 build and consumer test cover the complete compiler adapter.
"""
import errno
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "tools/bazel/0003-sonic-build-infra-configure-preprocessor.patch"


def added_code(path):
    for section in PATCH.read_text().split("diff --git ")[1:]:
        if section.splitlines()[0] == f"a/{path} b/{path}":
            return "\n".join(line[1:] for line in section.splitlines()
                             if line.startswith("+") and not line.startswith("+++")) + "\n"
    raise AssertionError(f"missing patch section: {path}")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux exec boundary")
class ConfigurePreprocessorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="configure-preprocessor-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = self.root / "transport.sh"
        self.helper.write_text(added_code("tools/build_tools/configure_preprocessor_flags.sh"))
        self.driver = self.root / ".sonic-compiler-argv"
        self.driver.write_text("#!/bin/sh\nset -eu\ncompiler=$1\nshift\n" +
                               added_code("tools/build_tools/configure_environment.sh") +
                               'exec "$compiler" "$@"\n')
        self.driver.chmod(0o755)
        self.recorder = self.root / "recorder.sh"
        self.recorder.write_text('#!/bin/sh\nexec "$RECORDER_PYTHON" "$RECORDER_SCRIPT" "$@"\n')
        self.recorder.chmod(0o755)
        recorder_script = self.root / "record.py"
        recorder_script.write_text("import json, os, sys\nfrom pathlib import Path\n"
                                   "Path(os.environ['RECORD']).write_text(json.dumps("
                                   "{'argv': sys.argv[1:], 'ldflags': os.environ.get('LDFLAGS')}))\n")
        self.record = self.root / "record.json"
        self.env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(self.root),
                    "RECORDER_PYTHON": sys.executable, "RECORDER_SCRIPT": str(recorder_script),
                    "RECORD": str(self.record), "LDFLAGS": "-L/unchanged -lkept"}
        self.entry = self.root / "configure.sh"
        self.entry.write_text('exec "$1" "$2" "$CPPFLAGS" tail\n')

    def transport(self, arguments):
        raw = self.root / "flags.txt"
        raw.write_text(shlex.join(arguments))
        # Match foreign_cc's command-local assignment to the source builtin.
        # The following exec also proves the outer shell regains its small env.
        return subprocess.run([
            "/bin/bash", "-eu", "-c",
            'CPPFLAGS=outer; export CPPFLAGS\n'
            'CPPFLAGS=$(<"$1") source "$2" "$3" /bin/sh "$4" "$5" "$6"\n'
            'test "$CPPFLAGS" = outer\n/bin/sh -c :\n',
            "transport-test", str(raw), str(self.helper), sys.executable,
            str(self.entry), str(self.driver), str(self.recorder),
        ], cwd=self.root, env=self.env, capture_output=True, text=True)

    def test_large_environment_fails_before_transport_and_succeeds_after(self):
        arguments = ["-iquote/" + "x" * 240 + f"/{i}/include" for i in range(600)]
        flags = shlex.join(arguments)
        self.assertGreater(len(flags), 138337)
        if os.sysconf("SC_PAGESIZE") == 4096:
            with self.assertRaises(OSError) as failure:
                subprocess.run(["/bin/sh", "-c", ":"], env={**self.env, "CPPFLAGS": flags})
            self.assertEqual(failure.exception.errno, errno.E2BIG)
        result = self.transport(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(self.record.read_text())
        self.assertEqual(captured["argv"], arguments + ["tail"])
        self.assertEqual(captured["ldflags"], self.env["LDFLAGS"])

    def test_argument_boundaries_order_and_ordinary_response_are_preserved(self):
        arguments = ["-iquote", "/quoted path", "-isystem/system's path", "", "-DVALUE=a\\b",
                     "-I/first", "-I/first", "@ordinary.rsp", 'literal$(not-a-command)']
        result = self.transport(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["argv"], arguments + ["tail"])

    def test_empty_flags(self):
        result = self.transport([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["argv"], ["tail"])

    def test_line_breaks_in_arguments_are_rejected(self):
        for value in ("bad\nargument", "bad\rargument"):
            with self.subTest(value=repr(value)):
                result = self.transport([value])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot contain line breaks", result.stderr)
                self.assertFalse(self.record.exists())

    def test_other_response_paths_remain_opaque(self):
        result = subprocess.run([str(self.driver), str(self.recorder), "@unrelated.rsp"],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["argv"], ["@unrelated.rsp"])

    def test_private_vector_requires_its_format_marker(self):
        vector = self.root / ".sonic-preprocessor-arguments"
        vector.write_text("wrong-format\n-I/path\n")
        result = subprocess.run([str(self.driver), str(self.recorder), "@" + str(vector)],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid private preprocessor argument response", result.stderr)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
