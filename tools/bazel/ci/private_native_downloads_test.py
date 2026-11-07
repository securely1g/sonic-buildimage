#!/usr/bin/env python3
"""Native private transport preserves APT bytes and suppresses private origins."""

import contextlib
import gzip
import http.client
import json
import os
import importlib.util
import io
from pathlib import Path
import threading
import types
import tempfile
import unittest
from unittest import mock
import urllib.error

SPEC = importlib.util.spec_from_file_location("native_downloads", Path(__file__).with_name("private_native_downloads.py"))
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
MIRRORS = {"DEBIAN_MIRROR_ROOT": "https://private.example.invalid/archive/debian/",
           "DEBIAN_SECURITY_MIRROR_ROOT": "https://private.example.invalid/archive/debian-security/"}
MANIFEST = {name: {"current": stamp, "snapshots": {stamp: {}}} for name, stamp in
            (("debian", "20260727T143429Z"), ("debian-security", "20260726T121236Z"))}


class Response(io.BytesIO):
    status = 200
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data)), "Content-Type": "application/octet-stream",
                        "Location": MIRRORS["DEBIAN_MIRROR_ROOT"], "Set-Cookie": "private-cookie"}


class NativeTransportTests(unittest.TestCase):
    def test_worker_sources_keep_public_hosts_snapshots_and_non_debian_tls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sources.list.d").mkdir()
            source = root / "sources.list"
            source.write_text("deb [signed-by=/keyring.gpg] https://snapshot.debian.org/archive/debian/20260727T143429Z trixie main\n")
            extra = root / "sources.list.d/docker.sources"
            extra.write_text("Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: trixie\n")
            original = extra.read_bytes()
            self.assertEqual(native.public_apt_transport(root), 1)
            self.assertIn("http://snapshot.debian.org/archive/debian/20260727T143429Z", source.read_text())
            self.assertIn("signed-by=/keyring.gpg", source.read_text())
            self.assertEqual(extra.read_bytes(), original)
            self.assertEqual(native.public_apt_transport(root), 0)

    def test_debian_and_security_keep_archive_path_and_select_reviewed_snapshot(self):
        for host, archive, key in (("deb.debian.org", "debian", "DEBIAN_MIRROR_ROOT"),
                                   ("security.debian.org", "debian-security", "DEBIAN_SECURITY_MIRROR_ROOT")):
            for suffix in ("dists/trixie/InRelease", "pool/main/a/audit/libaudit1_4.0.2-2+b2_amd64.deb"):
                with self.subTest(archive=archive, suffix=suffix):
                    self.assertEqual(native.destination(f"http://{host}/{archive}/{suffix}", MIRRORS, MANIFEST),
                        (MIRRORS[key] + MANIFEST[archive]["current"] + "/" + suffix, archive))

    def test_snapshot_timestamp_is_preserved_and_unreviewed_snapshot_rejected(self):
        uri = "http://snapshot.debian.org/archive/debian/20260727T143429Z/pool/main/a/file.deb"
        self.assertEqual(native.destination(uri, MIRRORS, MANIFEST),
            (MIRRORS["DEBIAN_MIRROR_ROOT"] + "20260727T143429Z/pool/main/a/file.deb", "debian"))
        with self.assertRaises(ValueError):
            native.destination(uri.replace("20260727", "20260728"), MIRRORS, MANIFEST)

    def test_tls_or_credential_bearing_proxy_requests_are_not_reinterpreted(self):
        for uri in ("https://deb.debian.org/debian/file", "http://user:secret@deb.debian.org/debian/file",
                    "http://deb.debian.org:443/debian/file", "file:///etc/passwd",
                    "http://deb.debian.org/debian/../../private", "http://deb.debian.org/debian/file?unexpected=1"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                native.destination(uri, MIRRORS, MANIFEST)

    def serve(self, opener):
        counts = {"debian": 0, "debian-security": 0}
        server = native.http.server.ThreadingHTTPServer(("127.0.0.1", 0), native.handler(MIRRORS, MANIFEST, counts, opener=opener))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port, counts

    def test_real_proxy_preserves_signed_bytes_but_not_private_response_headers(self):
        data = b"-----BEGIN PGP SIGNED MESSAGE-----\nSigned Debian index bytes\n"
        opener = mock.Mock()
        opener.open.return_value = Response(data)
        port, counts = self.serve(opener)
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        connection.request("GET", "http://deb.debian.org/debian/dists/trixie/InRelease",
                           headers={"Authorization": "Basic client-secret", "Range": "bytes=0-"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), data)
        self.assertIsNone(response.getheader("Location"))
        self.assertIsNone(response.getheader("Set-Cookie"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, MIRRORS["DEBIAN_MIRROR_ROOT"] + "20260727T143429Z/dists/trixie/InRelease")
        self.assertEqual(request.get_header("Range"), "bytes=0-")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(counts["debian"], 1)

    def test_private_redirect_and_error_body_never_reach_native_client(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(MIRRORS["DEBIAN_MIRROR_ROOT"], 302,
                                                        "private.example.invalid", None, None)
        port, _ = self.serve(opener)
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        connection.request("GET", "http://deb.debian.org/debian/dists/trixie/InRelease")
        response = connection.getresponse()
        self.assertEqual(response.status, 502)
        self.assertNotIn(b"private.example.invalid", response.read())
        self.assertIsNone(response.getheader("Location"))

    def test_connect_does_not_bypass_verified_upstream_tls(self):
        opener = mock.Mock()
        port, _ = self.serve(opener)
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        connection.request("CONNECT", "deb.debian.org:443")
        self.assertEqual(connection.getresponse().status, 405)
        opener.open.assert_not_called()

    def test_conditional_metadata_refresh_preserves_304_without_private_headers(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(MIRRORS["DEBIAN_MIRROR_ROOT"], 304, "Not Modified",
            {"ETag": '"index-hash"', "Location": MIRRORS["DEBIAN_MIRROR_ROOT"]}, None)
        port, _ = self.serve(opener)
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        connection.request("GET", "http://deb.debian.org/debian/dists/trixie/InRelease", headers={"If-None-Match": '"index-hash"'})
        response = connection.getresponse()
        self.assertEqual(response.status, 304)
        self.assertEqual(response.read(), b"")
        self.assertEqual(response.getheader("ETag"), '"index-hash"')
        self.assertIsNone(response.getheader("Location"))

    def test_bazel_wrapper_preserves_startup_flags_and_disables_caches_last(self):
        arguments = ["--output_user_root=/actions/bazel/output", "build", "--disk_cache=/old", "//:target"]
        actual = native.bazel_command(arguments, Path("/private/downloader.cfg"), "/usr/bin/bazel")
        self.assertEqual(actual[:len(arguments) + 1], ["/usr/bin/bazel", *arguments])
        self.assertEqual(actual[-3:], ["--experimental_downloader_config=/private/downloader.cfg", "--disk_cache=", "--repository_cache="])
        for arguments in (["version"], ["--version"], ["help", "build"], ["shutdown"]):
            self.assertEqual(native.bazel_command(arguments, Path("/private/config"), "/bazel"), ["/bazel", *arguments])
        arguments = ["run", "//:program", "--", "build", "--user-argument"]
        actual = native.bazel_command(arguments, Path("/private/config"), "/bazel")
        self.assertEqual(actual[-3:], arguments[-3:])
        self.assertEqual(actual[actual.index("--") - 1], "--repository_cache=")


class AlternativesPrivacyTests(unittest.TestCase):
    member = "var/lib/dpkg/alternatives/builtins.7.gz"
    metadata = b"auto\n/usr/share/man/man7/builtins.7.gz\n\n/usr/share/man/man7/bash-builtins.7.gz\n10\n\n"

    class Scanner:
        def _scan_stream(self, stream, name="", depth=0):
            payload = stream.read()
            if name.endswith(".gz"):
                payload = gzip.decompress(payload)
            if b"private.example.invalid" in payload:
                raise ValueError("Private location remains")
            return payload

    def scan(self, payload, member=None):
        scanner = native.native_redactor(self.Scanner)()
        return scanner._scan_stream(io.BytesIO(payload), member or self.member, 4)

    def test_real_alternatives_layout_is_scanned_unchanged_as_text(self):
        self.assertTrue(native.alternatives_metadata(self.metadata))
        self.assertEqual(self.scan(self.metadata), self.metadata)
        # Slave links may be absent for one of the selected alternatives.
        linked = b"manual\n/usr/bin/editor\neditor.1.gz\n/usr/share/man/man1/editor.1.gz\n\n/bin/nano\n40\n\n\n/bin/vi\n30\n/usr/share/man/man1/vi.1.gz\n\n"
        self.assertTrue(native.alternatives_metadata(linked))
        self.assertEqual(self.scan(linked), linked)

    def test_private_location_in_valid_metadata_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "Private location"):
            self.scan(self.metadata.replace(b"/usr/share/man/man7/bash", b"/private.example.invalid/bash"))

    def test_malformed_metadata_and_real_archives_keep_normal_scanning(self):
        for payload in (self.metadata.replace(b"\n10\n", b"\ninvalid\n"),
                        self.metadata + b"\x00", b"\x1f\x8btruncated"):
            with self.subTest(payload=payload), self.assertRaises((OSError, EOFError)):
                self.scan(payload)
        self.assertEqual(self.scan(gzip.compress(b"real archive payload")), b"real archive payload")
        with self.assertRaises(OSError):
            self.scan(self.metadata, "usr/share/man/man7/builtins.7.gz")


class WrapperLifecycleTests(unittest.TestCase):
    def test_managed_kernel_options_are_forwarded_and_private_files_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helpers = root / "helpers/ci"
            helpers.mkdir(parents=True)
            managed = root / "managed.bazelrc"
            managed.write_text("common --remote_cache=https://remote.example.invalid/\n")
            artifacts = root / "artifacts"
            seen = []
            class Redactor:
                def __init__(self, values):
                    seen.extend(values)
                def mask_github(self):
                    pass
                def redact(self, line):
                    return line
                def scrub_and_scan(self, path):
                    pass
            private_mirrors = types.SimpleNamespace(
                selected_mirrors=lambda _: MIRRORS,
                load_manifest=lambda: MANIFEST,
                configuration=lambda *args, **kwargs: "private fixture policy",
                apt_mirror_map=lambda *args, **kwargs: {"public": "private"},
                write_config=lambda path, value: path.write_text(value),
                child_environment=lambda: {key: value for key, value in os.environ.items() if key not in MIRRORS})
            args = types.SimpleNamespace(helpers=helpers, artifacts=artifacts,
                private_dir=root / "private", phase="verification", check_artifacts=False,
                command=[native.sys.executable, "-c",
                    "import json,os,pathlib; "
                    "assert 'DEBIAN_MIRROR_ROOT' not in os.environ; "
                    "assert 'DEBIAN_SECURITY_MIRROR_ROOT' not in os.environ; "
                    "assert os.environ['SONIC_BAZEL_REMOTE_ONLY_RC'] == " + repr(str(managed)) + "; "
                    "assert pathlib.Path(os.environ['SONIC_BAZEL_DOWNLOADER_CONFIG']).read_text() == 'private fixture policy'; "
                    "assert os.environ['BAZEL_CONTAINER_CACHE_DIR'] == ''; "
                    "assert os.environ['HTTP_PROXY'].startswith('http://192.168.240.1:')"])
            child_check = args.command[-1]
            clean_environment = {key: value for key, value in os.environ.items()
                                 if key not in {"https_proxy", "HTTPS_PROXY"}}
            proxy_url = "http://configured-proxy.example.invalid:3128"
            for inherited, expected in (({}, ""), ({"HTTPS_PROXY": proxy_url}, proxy_url),
                                        ({"https_proxy": proxy_url}, proxy_url),
                                        ({"https_proxy": "", "HTTPS_PROXY": proxy_url}, "")):
                args.command[-1] = (child_check + "; assert os.environ['https_proxy'] == " + repr(expected)
                                    + "; assert os.environ['HTTPS_PROXY'] == " + repr(expected))
                with self.subTest(inherited=inherited), \
                     mock.patch.dict(native.sys.modules, {"private_mirrors": private_mirrors,
                         "mirror_privacy": types.SimpleNamespace(Redactor=Redactor)}), \
                     mock.patch.dict(os.environ, {**clean_environment, **MIRRORS, **inherited,
                         "SONIC_BAZEL_REMOTE_ONLY_RC": str(managed)}, clear=True), \
                     mock.patch.object(native.subprocess, "check_output", return_value=native.REGISTRY_REVISION + "\n"), \
                     mock.patch.object(native.shutil, "which", return_value="/bin/true"), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(native.run(args), 0)
            self.assertEqual(list(args.private_dir.iterdir()), [])
            self.assertIn("https://remote.example.invalid/", seen)
            receipt = json.loads((artifacts / "private-downloads-verification.json").read_text())
            self.assertNotIn("example.invalid", json.dumps(receipt))
            self.assertFalse(receipt["local_bazel_caches"])


if __name__ == "__main__":
    unittest.main()
