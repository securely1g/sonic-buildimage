"""Keep managed cache selection private and bind it to the runner runtime."""

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel import build_helpers
from tools.bazel.cache import managed


ORIGIN = "https://cache.example.invalid:8443/"


class ManagedCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.json"
        self.value = {"settings": {"endpoint": ORIGIN, "host": "cache.example.invalid"},
                      "response": {"headers": {}}}
        self.save()
        helper = self.root / "helper.py"
        helper.write_text(
            "import json,sys\n"
            f"value=json.load(open({str(self.config)!r}))\n"
            "if sys.argv[1]=='settings': result=value['settings']\n"
            "else:\n"
            " request=json.load(sys.stdin)\n"
            " assert request['uri'].startswith(value['settings']['endpoint'])\n"
            " result=value['response']\n"
            "print(json.dumps(result))\n"
        )
        self.launcher = self.root / "bazel"
        self.launcher.write_text("launcher")
        for target, value in (("CONFIG", self.config), ("HELPER", helper),
                              ("RUNTIME_FILES", (self.config, helper, self.launcher)),
                              ("RUNTIME_DIRECTORIES", (self.root / "optional-compat",))):
            setting = patch.object(managed, target, value)
            setting.start()
            self.addCleanup(setting.stop)
        environment = patch.dict(os.environ, {
            managed.FLAG: "1",
            "GITHUB_ACTIONS": "true", "BAZEL_REMOTE_AUTH": "must-not-be-forwarded",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def save(self):
        self.config.write_text(json.dumps(self.value))

    def test_selection_retains_only_cache_and_runner_identities(self):
        receipt = managed.selection()
        self.assertEqual(receipt["cache_mode"], "runner-managed-anonymous")
        self.assertEqual(receipt["cache_identity_sha256"], hashlib.sha256(ORIGIN.encode()).hexdigest())
        self.assertRegex(receipt["cache_runtime_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("cache.example.invalid", json.dumps(receipt))
        self.launcher.write_text("changed launcher")
        self.assertNotEqual(managed.selection()["cache_runtime_sha256"], receipt["cache_runtime_sha256"])

    def test_invalid_or_authenticated_selection_fails_without_details(self):
        cases = [
            {"settings": {"endpoint": "http://cache.example.invalid/", "host": "cache.example.invalid"}, "response": {"headers": {}}},
            {"settings": {"endpoint": ORIGIN, "host": "other.example.invalid"}, "response": {"headers": {}}},
            {"settings": {"endpoint": ORIGIN, "host": "cache.example.invalid"}, "response": {"headers": {"Authorization": ["private-test-value"]}}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.value = value
                self.save()
                with self.assertRaises(ValueError) as error:
                    managed.selection()
                self.assertEqual(str(error.exception), managed.ERROR)

    def test_missing_config_or_runtime_asset_does_not_fall_back(self):
        self.config.unlink()
        with self.assertRaisesRegex(ValueError, managed.ERROR):
            managed.selection()
        self.save()
        self.launcher.unlink()
        with self.assertRaisesRegex(ValueError, managed.ERROR):
            managed.selection()
        os.environ[managed.FLAG] = "unexpected"
        with self.assertRaisesRegex(ValueError, managed.ERROR):
            managed.enabled()

    def test_workflow_masks_precede_safe_receipt_output(self):
        output = self.root / "public/selection.json"
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(managed, "_probe", return_value={"cache_access": True, "http_status": 200}):
            managed.prepare(output, github_mask=True)
        self.assertEqual(stream.getvalue().splitlines(), [
            "::add-mask::" + ORIGIN,
            "::add-mask::cache.example.invalid",
            "Validated runner-managed anonymous cache access",
        ])
        self.assertNotIn("cache.example.invalid", output.read_text())
        self.assertEqual(json.loads(output.read_text()), {**managed.selection(), "cache_access": True, "http_status": 200})
        os.environ.pop("GITHUB_ACTIONS")
        with self.assertRaisesRegex(ValueError, managed.ERROR):
            managed.prepare(output, github_mask=True)

    def test_access_probe_sends_anonymous_bounded_head_and_keeps_safe_errors(self):
        response = type("Response", (), {"status": 200, "__enter__": lambda self: self,
                                         "__exit__": lambda self, *args: None})()
        with patch.object(managed.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(managed._probe(ORIGIN), {"cache_access": True, "http_status": 200})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "HEAD")
            self.assertFalse(request.has_header("Authorization"))
            self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 15})
            build.return_value.open.side_effect = urllib.error.HTTPError(ORIGIN, 403, "private-test-value", None, None)
            failure = managed._probe(ORIGIN)
            self.assertEqual(failure, {"cache_access": False, "error_type": "HTTPError", "http_status": 403})
            self.assertNotIn("cache.example.invalid", json.dumps(failure))

    def test_runtime_mounts_require_every_launcher_asset(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        binary = runtime / "bazel"
        binary.touch()
        policy = runtime / "policy"
        policy.mkdir()
        downloader = runtime / "mirrors.cfg"
        downloader.touch()
        os.environ[managed.DOWNLOADER_FLAG] = str(downloader)
        with patch.object(managed, "RUNTIME_FILES", (binary,)), \
                patch.object(managed, "RUNTIME_DIRECTORIES", (policy,)):
            self.assertEqual(managed.runtime_paths(), (str(binary), str(policy), str(downloader)))
            (policy / "target").mkdir()
            link = policy / "active"
            link.symlink_to("target", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, managed.ERROR):
                managed.runtime_paths()
            link.unlink()
            binary.unlink()
            with self.assertRaisesRegex(ValueError, managed.ERROR):
                managed.runtime_paths()

    def test_read_only_options_do_not_publish_or_override_the_origin(self):
        self.assertEqual(build_helpers.remote_cache_options(), [
            "--remote_executor=", "--remote_accept_cached=true",
            "--remote_upload_local_results=false",
        ])
        managed.check_options(["--jobs=4", "--remote_accept_cached=false",
                               "--remote_upload_local_results=true"])
        for option in ("--remote_cache=https://other.example/", "--remote_cache",
                       "--credential_helper=other.example=/tmp/helper",
                       "--remote_header=Authorization=private-test-value",
                       "--remote_cache_header=Authorization=private-test-value", "--config=other"):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, managed.ERROR):
                managed.check_options([option])


if __name__ == "__main__":
    unittest.main()
