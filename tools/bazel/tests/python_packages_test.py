"""Require complete native test evidence for the Python wheels retained by CI."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import python_packages


class PythonPackageEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.directory = self.workspace / "artifacts"
        self.revision = {"tested_head": "tested-revision", "architecture": "amd64"}
        self.wheel_receipt = {
            "status": "passed", "architecture": "x86_64", "python": "3.13.9",
            "wheels": [{"filename": name, "sha256": hashlib.sha256(name.encode()).hexdigest()}
                       for name in python_packages.WHEELS],
        }

    def fixture(self, *, zipped=False):
        for target in python_packages.TESTS:
            package, name = target.removeprefix("//").split(":")
            source = self.workspace / "bazel-testlogs" / package / name
            source.mkdir(parents=True)
            (source / "test.log").write_text("Test passed\n")
            (source / "test.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                '<testcase name="test_example"/></testsuite>\n')
            contents = {}
            if name.endswith("_full_test"):
                contents["pytest-inventory.json"] = json.dumps({
                    "python": "3.13.9", "collected": 1, "tests": ["test_example"],
                })
            if name == "sonic_python_wheels_test":
                contents["wheels.json"] = json.dumps(self.wheel_receipt)
            if contents:
                outputs = source / "test.outputs"
                outputs.mkdir()
                if zipped:
                    with zipfile.ZipFile(outputs / "outputs.zip", "w") as archive:
                        for filename, content in contents.items():
                            archive.writestr(filename, content)
                else:
                    for filename, content in contents.items():
                        (outputs / filename).write_text(content)

    def collect(self):
        def export(workspace, directory, receipt, targets, **kwargs):
            paths = {}
            for name in targets:
                paths[name] = directory / name
                paths[name].write_bytes(name.encode())
            return paths

        with mock.patch.object(python_packages.build, "collect_archives", side_effect=export), \
             mock.patch.object(python_packages.platform, "machine", return_value="x86_64"):
            return python_packages.collect(self.workspace, self.directory, self.revision, "amd64")

    def test_retain_native_results_with_direct_outputs(self):
        """Preserve each required result, collected test inventory and matching wheel hash."""
        self.fixture()
        receipt = self.collect()
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["revision"], self.revision)
        self.assertEqual(set(receipt["tests"]), set(python_packages.TESTS))
        self.assertEqual(json.loads((self.directory / "wheels.json").read_text()), self.wheel_receipt)
        for target, evidence in receipt["tests"].items():
            if target.endswith("_full_test"):
                self.assertEqual(json.loads((self.directory / evidence["inventory"]["path"]).read_text())["collected"], 1)

    def test_retain_bazel_zipped_outputs(self):
        """Read the same required receipts when Bazel archives undeclared outputs."""
        self.fixture(zipped=True)
        self.assertEqual(self.collect()["status"], "passed")

    def test_missing_xml_fails_and_records_failure(self):
        """An artifact upload cannot turn missing source-suite evidence into success."""
        self.fixture()
        (self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.xml").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required.*test.xml"):
            self.collect()
        self.assertEqual(json.loads((self.directory / "receipt.json").read_text())["status"], "failed")

    def test_missing_inventory_fails(self):
        """A passing test log alone does not establish complete pytest collection."""
        self.fixture()
        (self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_config_engine_full_test/test.outputs/pytest-inventory.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.collect()
        self.assertEqual(json.loads((self.directory / "receipt.json").read_text())["status"], "failed")

    def test_failed_xml_fails(self):
        """An existing report containing test failures cannot receive a passing receipt."""
        self.fixture()
        report = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.xml"
        report.write_text('<testsuite tests="1" failures="1"><testcase name="failed">'
                          '<failure message="failed"/></testcase></testsuite>')
        with self.assertRaisesRegex(ValueError, "unsuccessful test report"):
            self.collect()

    def test_xml_error_with_wrong_summary_fails(self):
        """An error element remains fatal even if its suite summary claims success."""
        self.fixture()
        report = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.xml"
        report.write_text('<testsuite tests="1" errors="0"><testcase name="error">'
                          '<error message="failed"/></testcase></testsuite>')
        with self.assertRaisesRegex(ValueError, "unsuccessful test report"):
            self.collect()

    def test_incomplete_xml_fails(self):
        """Summary counts must describe actual case results retained in the report."""
        self.fixture()
        report = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.xml"
        report.write_text('<testsuite tests="2"><testcase name="only_one"/></testsuite>')
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.collect()

    def test_inventory_count_must_match_xml(self):
        """A valid report for a smaller run cannot represent the collected full suite."""
        self.fixture()
        inventory = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.outputs/pytest-inventory.json"
        inventory.write_text(json.dumps({"python": "3.13.9", "collected": 2,
                                         "tests": ["test_example", "test_other"]}))
        with self.assertRaisesRegex(ValueError, "inventory does not match"):
            self.collect()

    def test_inventory_must_include_each_collected_case(self):
        """The retained inventory must name every case represented by its count."""
        self.fixture()
        inventory = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.outputs/pytest-inventory.json"
        inventory.write_text(json.dumps({"python": "3.13.9", "collected": 1, "tests": []}))
        with self.assertRaisesRegex(ValueError, "inventory does not match"):
            self.collect()

    def test_source_suite_python_must_match(self):
        """A source result from another interpreter cannot represent native Python 3.13."""
        self.fixture()
        inventory = self.workspace / "bazel-testlogs/tools/bazel/tests/sonic_py_common_full_test/test.outputs/pytest-inventory.json"
        inventory.write_text(json.dumps({"python": "3.12.9", "collected": 1,
                                         "tests": ["test_example"]}))
        with self.assertRaisesRegex(ValueError, "different Python version"):
            self.collect()

    def test_different_wheel_bytes_fail(self):
        """Retained wheels must be the bytes exercised by the installation test."""
        self.wheel_receipt["wheels"][0]["sha256"] = "0" * 64
        self.fixture()
        with self.assertRaisesRegex(ValueError, "Retained wheels differ"):
            self.collect()

    def test_other_architecture_receipt_fails(self):
        """A successful result from another architecture cannot stand in for this run."""
        self.wheel_receipt["architecture"] = "aarch64"
        self.fixture()
        with self.assertRaisesRegex(ValueError, "successful native run"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
