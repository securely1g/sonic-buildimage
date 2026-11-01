"""Exercise native Docker retries and real APT index/download failure handling."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[3]
APT_CONFIG = ROOT / "files/apt/apt.conf.d/apt-timeout-n-retries"


@unittest.skipUnless(shutil.which("apt-get"), "requires the native Debian/Ubuntu APT client")
class AptIndexRefreshTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="apt-index-refresh-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        for path in ("etc/apt.conf.d", "etc/sources.list.d", "state/lists/partial",
                     "cache/archives/partial", "log"):
            (self.work / path).mkdir(parents=True, exist_ok=True)
        (self.work / "state/status").touch()
        configuration = self.work / "isolated.conf"
        configuration.write_text(
            f'Dir::Etc "{self.work / "etc"}";\n'
            f'Dir::State "{self.work / "state"}";\n'
            f'Dir::State::status "{self.work / "state/status"}";\n'
            f'Dir::Cache "{self.work / "cache"}";\n'
            f'Dir::Log "{self.work / "log"}";\n'
            'Acquire::http::Proxy "DIRECT";\n')
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.lower().endswith("_proxy")}
        self.environment.update(APT_CONFIG=str(configuration), LC_ALL="C")

    def update(self, strict):
        command = [shutil.which("apt-get")]
        if strict:
            command += ["-c", str(APT_CONFIG)]
        command += ["-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=1", "update"]
        return subprocess.run(command, env=self.environment, capture_output=True,
                              text=True, timeout=15)

    def test_unreachable_index_cannot_be_a_successful_docker_layer(self):
        # A bound, non-listening loopback socket refuses connections and keeps
        # the port reserved. No external endpoint or host APT state is used.
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            port = endpoint.getsockname()[1]
            (self.work / "etc/sources.list").write_text(
                f"deb [trusted=yes] http://127.0.0.1:{port} fixture main\n")
            baseline = self.update(strict=False)
            self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
            self.assertIn("Failed to fetch", baseline.stderr)
            strict = self.update(strict=True)
            self.assertEqual(strict.returncode, 100, strict.stdout + strict.stderr)
            self.assertIn("Failed to fetch", strict.stderr)

    def test_valid_local_index_still_succeeds(self):
        repository = self.work / "repository"
        distribution = repository / "dists/fixture"
        packages = distribution / "main/binary-amd64/Packages"
        packages.parent.mkdir(parents=True)
        packages.write_bytes(b"")
        (distribution / "Release").write_text(
            "Suite: fixture\nCodename: fixture\nArchitectures: amd64\nComponents: main\n"
            "SHA256:\n"
            " e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 0 main/binary-amd64/Packages\n")
        (self.work / "etc/sources.list").write_text(
            f"deb [trusted=yes arch=amd64] {repository.as_uri()} fixture main\n")
        result = self.update(strict=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Failed to fetch", result.stderr)

    def download_fixture(self, failures, failure_status=503):
        if not shutil.which("dpkg-deb"):
            self.skipTest("requires dpkg-deb to read the existing fixture metadata")
        fixture = ROOT / "tools/bazel/tests/fixtures/oci-debs/example-gz_1.0-1_all.deb"
        payload = fixture.read_bytes()
        control = subprocess.check_output(
            ["dpkg-deb", "--field", str(fixture)], text=True, timeout=10)
        index = (control.rstrip() + f"\nFilename: pool/{fixture.name}\n"
                 f"Size: {len(payload)}\nSHA256: {hashlib.sha256(payload).hexdigest()}\n\n").encode()
        release = ("Suite: fixture\nCodename: fixture\nSHA256:\n "
                   f"{hashlib.sha256(index).hexdigest()} {len(index)} Packages\n").encode()
        endpoints = {"/Release": release, "/Packages": index}
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_GET(self):
                # Fixed in-memory endpoints only; never serve filesystem paths.
                path = str(PurePosixPath(self.path))
                if path == "/pool/" + fixture.name:
                    status = failure_status if len(requests) < failures else 200
                    requests.append(status)
                    body = payload if status == 200 else b"controlled fixture failure\n"
                else:
                    status = 200 if path in endpoints else 404
                    body = endpoints.get(path, b"fixture endpoint unavailable\n")
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            (self.work / "etc/sources.list").write_text(
                f"deb [trusted=yes] http://127.0.0.1:{server.server_port} ./\n")
            update = self.update(strict=True)
            self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
            # Exercise the production retry count. Disable only delays in this
            # isolated fixture so exhausted attempts do not slow the test suite.
            result = subprocess.run(
                [shutil.which("apt-get"), "-c", str(APT_CONFIG),
                 "-o", "Acquire::Retries::Delay=false", "download", "example-gz=1.0-1"],
                cwd=self.work, env=self.environment, capture_output=True, text=True, timeout=15)
            return result, requests, list(self.work.glob("*.deb")), payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "fixture server must stop")

    def test_package_download_recovers_after_four_transient_failures(self):
        result, requests, downloads, payload = self.download_fixture(failures=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(requests, [503] * 4 + [200])
        self.assertEqual(len(downloads), 1)
        self.assertEqual(hashlib.sha256(downloads[0].read_bytes()).hexdigest(),
                         hashlib.sha256(payload).hexdigest())

    def test_package_download_exhaustion_is_bounded_and_fails(self):
        result, requests, downloads, _ = self.download_fixture(failures=100)
        self.assertEqual(result.returncode, 100, result.stdout + result.stderr)
        self.assertEqual(requests, [503] * 9)  # Initial attempt plus eight retries.
        self.assertEqual(downloads, [])

    def test_package_download_permanent_forbidden_is_not_retried(self):
        result, requests, downloads, _ = self.download_fixture(failures=100, failure_status=403)
        self.assertEqual(result.returncode, 100, result.stdout + result.stderr)
        self.assertEqual(requests, [403])
        self.assertEqual(downloads, [])


if __name__ == "__main__":
    unittest.main()
