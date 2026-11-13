import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import observe_native_apt_hook as observer


class EncryptedHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root / "outputs"
        self.parent.mkdir(mode=0o700)
        self.certificate = b"public fixture certificate"
        self.recipient = self.root / "recipient.pem"
        self.recipient.write_bytes(self.certificate)
        self.plaintext = b"#!/bin/sh\n# private fixture text must never be published\n"
        self.metadata = {"bytes": len(self.plaintext), "sha256": hashlib.sha256(self.plaintext).hexdigest(),
                         "uid": 0, "mode": 0o555}
        for name, value in (("HOOK_SIZE", len(self.plaintext)), ("HOOK_SHA", self.metadata["sha256"]),
                            ("RECIPIENT_SIZE", len(self.certificate)),
                            ("RECIPIENT_SHA", hashlib.sha256(self.certificate).hexdigest())):
            patch = mock.patch.object(observer, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def invoke(self, *, source=None, metadata=None, result=None, error=None):
        result = result or SimpleNamespace(returncode=0, stdout=b"encrypted fixture ciphertext", stderr=b"")
        with mock.patch.object(observer.policy, "read_public_file", return_value=(self.plaintext if source is None else source, metadata or self.metadata)) as read, mock.patch.object(observer.policy, "inspect", side_effect=AssertionError("No policy/settings inspection")), mock.patch.object(subprocess, "run", side_effect=error, return_value=result) as run:
            value = observer.observe(self.recipient, self.parent)
        return value, read, run

    def test_only_ciphertext_and_public_hashes_are_published(self):
        value, read, run = self.invoke()
        read.assert_called_once_with(observer.HOOK, observer.policy.MOUNT, len(self.plaintext))
        self.assertEqual({p.name for p in (self.parent / "execution").iterdir()}, {"encrypted.cms", "lineage.json"})
        self.assertEqual(value, json.loads((self.parent / "execution/lineage.json").read_text()))
        self.assertFalse(value["policy_admitted"])
        for path in (self.parent / "execution").iterdir():
            self.assertNotIn(self.plaintext, path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        call = run.call_args
        self.assertEqual(call.kwargs["input"], self.plaintext)
        self.assertNotIn(self.plaintext.decode(), " ".join(call.args[0]))
        self.assertEqual(call.args[0][:8], ["/usr/bin/openssl", "cms", "-encrypt", "-binary", "-aes-256-gcm", "-outform", "DER", "-recip"])
        self.assertEqual(call.kwargs["timeout"], 20)
        self.assertEqual(call.kwargs["pass_fds"], (int(call.args[0][-1].rsplit("/", 1)[1]),))

    def test_original_size_and_hash_must_both_match(self):
        for source in (self.plaintext + b"x", b"x" * len(self.plaintext)):
            with self.subTest(source=source), mock.patch.object(observer, "encrypt") as run, self.assertRaises(ValueError):
                self.invoke(source=source)
            run.assert_not_called()
        self.assertFalse((self.parent / "execution").exists())

    def test_observed_owner_and_mode_are_not_relaxed(self):
        for changes in ({"uid": 1000}, {"mode": 0o755}, {"sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.invoke(metadata={**self.metadata, **changes})

    def test_unreviewed_recipient_stops_before_source_read(self):
        self.recipient.write_bytes(b"x" * len(self.certificate))
        with mock.patch.object(observer.policy, "read_public_file") as read, self.assertRaises(ValueError):
            observer.observe(self.recipient, self.parent)
        read.assert_not_called()

    def test_recipient_symlink_is_rejected(self):
        original = self.root / "original.pem"
        self.recipient.rename(original)
        self.recipient.symlink_to(original)
        with self.assertRaises(OSError):
            self.invoke()

    def test_recipient_fifo_is_rejected_without_blocking(self):
        self.recipient.unlink()
        os.mkfifo(self.recipient, 0o600)
        with self.assertRaises(ValueError):
            self.invoke()

    def test_openssl_failure_or_oversized_output_has_no_artifact(self):
        for result in (SimpleNamespace(returncode=1, stdout=b"", stderr=self.plaintext),
                       SimpleNamespace(returncode=0, stdout=b"x" * 65537, stderr=b"")):
            with self.subTest(returncode=result.returncode), self.assertRaises(ValueError):
                self.invoke(result=result)
        self.assertFalse((self.parent / "execution").exists())

    def test_encryption_timeout_has_no_artifact(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke(error=subprocess.TimeoutExpired("openssl", 20))
        self.assertFalse((self.parent / "execution").exists())

    def test_existing_output_is_preserved_before_source_read(self):
        output = self.parent / "execution"
        output.mkdir()
        (output / "owned").write_bytes(b"preserve")
        with mock.patch.object(observer.policy, "read_public_file") as read, self.assertRaises(ValueError):
            observer.observe(self.recipient, self.parent)
        read.assert_not_called()
        self.assertEqual((output / "owned").read_bytes(), b"preserve")

    def test_certificate_bytes_are_piped_without_reopening_the_path(self):
        def encrypt(command, **kwargs):
            self.assertEqual(os.read(kwargs["pass_fds"][0], 4096), self.certificate)
            self.assertEqual(kwargs["input"], self.plaintext)
            return SimpleNamespace(returncode=0, stdout=b"encrypted", stderr=b"")
        with mock.patch.object(subprocess, "run", side_effect=encrypt):
            self.assertEqual(observer.encrypt(self.plaintext, self.certificate), b"encrypted")


if __name__ == "__main__":
    unittest.main()
