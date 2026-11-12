"""Check that Make's preflight requires a successful read-only CAS lookup."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from tools.bazel.ci import probe_remote_cache as subject


class Unavailable(Exception):
    def code(self):
        from types import SimpleNamespace
        return SimpleNamespace(name="UNAVAILABLE")


class ProbeTest(unittest.TestCase):
    def test_lookup_and_receipt(self):
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        request = bytes.fromhex("12420a40") + digest.encode("ascii")
        # A missing digest is returned in field 2 of FindMissingBlobsResponse.
        for result, status in ((b"", "OK"), (request, "UNEXPECTED_FIND_MISSING_BLOBS_RESPONSE"),
                               (Unavailable(), "UNAVAILABLE")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                grpc = MagicMock()
                grpc.RpcError = Unavailable
                channel = grpc.insecure_channel.return_value.__enter__.return_value
                lookup = channel.unary_unary.return_value
                if isinstance(result, Exception):
                    lookup.side_effect = result
                else:
                    lookup.return_value = result
                output = Path(directory) / "artifacts" / "cache-access.json"
                self.assertEqual(subject.probe(grpc, output), status == "OK")
                grpc.insecure_channel.assert_called_once_with(subject.PRIVATE_REMOTE_CACHE.split("://", 1)[1])
                channel.unary_unary.assert_called_once_with(subject.FIND_MISSING)
                lookup.assert_called_once_with(request, timeout=15, wait_for_ready=True)
                self.assertEqual(json.loads(output.read_bytes()), {
                    "endpoint": subject.PRIVATE_REMOTE_CACHE, "digest": digest, "status": status})


if __name__ == "__main__":
    unittest.main()
