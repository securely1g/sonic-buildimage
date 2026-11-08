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
import re
import threading
import types
import tempfile
import unittest
from unittest import mock
import urllib.error

SPEC = importlib.util.spec_from_file_location("native_downloads", Path(__file__).with_name("private_native_downloads.py"))
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
native.sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    def test_input_diagnostics_export_only_vetted_facts_after_privacy_check(self):
        from resolution import graph_diagnostics
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory) / "repository-inputs"
            artifacts.mkdir()
            secret = "private-credential-and-path"
            graph = graph_diagnostics("", "ERROR: HTTP 504 " + secret, 37)
            (artifacts / "module-graph-diagnostics.json").write_text(json.dumps(graph))
            (artifacts / "module-graph.stderr").write_text(secret)
            (artifacts / "private-downloads-inputs.json").write_text(json.dumps({
                "not_for_export": secret,
                "github_release_transport": {"authenticated": True, "completed": 2, "failed": 1,
                    "failure_categories": {"metadata:http:403:rate_limited": 1}, "not_for_export": secret},
                "github_commit_archive_transport": {"authenticated": False, "completed": 3, "failed": 0,
                    "failure_categories": {}, "not_for_export": secret}}))
            redactor = mock.Mock()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                destination = native.export_input_diagnostics(artifacts, redactor)
            redactor.scrub_and_scan.assert_called_once_with(destination)
            self.assertEqual([p.name for p in destination.iterdir()], ["diagnostics.json"])
            exported = (destination / "diagnostics.json").read_text()
            self.assertNotIn(secret, exported + output.getvalue())
            self.assertEqual(json.loads(exported)["graph"]["returncode"], 37)
            self.assertEqual(json.loads(exported)["github_release_transport"]["failed"], 1)
            self.assertEqual(json.loads(exported)["github_commit_archive_transport"],
                             {"authenticated": False, "completed": 3, "failed": 0, "failure_categories": {}})

    def test_archive_diagnostics_cannot_claim_authenticated_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory) / "repository-inputs"
            artifacts.mkdir()
            counters = {"authenticated": True, "completed": 1, "failed": 0, "failure_categories": {}}
            (artifacts / "private-downloads-inputs.json").write_text(json.dumps({
                "github_release_transport": counters, "github_commit_archive_transport": counters}))
            redactor = mock.Mock()
            with self.assertRaises(ValueError):
                native.export_input_diagnostics(artifacts, redactor)
            redactor.scrub_and_scan.assert_not_called()
            self.assertFalse((Path(directory) / "native-input-diagnostics").exists())

    def test_input_diagnostics_reject_arbitrary_strings_and_privacy_failure(self):
        from resolution import graph_diagnostics
        for invalid in (True, False):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                artifacts = Path(directory) / "repository-inputs"
                artifacts.mkdir()
                graph = graph_diagnostics("", "ERROR: HTTP 504", 8)
                if invalid:
                    graph["signals"] = ["https://secret.invalid/credential"]
                (artifacts / "module-graph-diagnostics.json").write_text(json.dumps(graph))
                redactor = mock.Mock()
                redactor.scrub_and_scan.side_effect = ValueError("Privacy check failed")
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                    native.export_input_diagnostics(artifacts, redactor)
                self.assertEqual(output.getvalue(), "")
                if invalid:
                    self.assertFalse((Path(directory) / "native-input-diagnostics").exists())
                    redactor.scrub_and_scan.assert_not_called()

    def test_bridge_proxy_cannot_relay_to_release_listener(self):
        for host in ("127.0.0.1", "localhost", "2130706433"):
            for prefix in ("/_sonic_github_release/", "/_sonic_github_archive/"):
                with self.subTest(host=host, prefix=prefix), self.assertRaisesRegex(ValueError, "direct loopback"):
                    native.destination(f"http://{host}{prefix}github.com/example/repo/releases/download/v1/a.tar.gz",
                                       MIRRORS, MANIFEST)

    def test_gawk_mirror_preserves_original_sources_and_other_download_rules(self):
        policy = "rewrite snapshot.example.invalid/(.*) https://private.example.invalid/$1\n"
        configured = native.native_source_mirrors(policy)
        preferred, fallback, unchanged = configured.splitlines()
        self.assertEqual(unchanged + "\n", policy)
        pattern = preferred.split()[1]
        for host in ("mirrors.kernel.org", "ftp.gnu.org", "ftpmirror.gnu.org"):
            original = host + "/gnu/gawk/gawk-5.3.2.tar.xz"
            match = re.fullmatch(pattern, original)
            self.assertIsNotNone(match)
            self.assertEqual(preferred.split()[2], native.GAWK_MIRROR)
            self.assertEqual(fallback.split()[1], pattern)
            self.assertEqual(fallback.split()[2].replace("$1", match[1]), "https://" + original)
        for other in ("ftp.gnu.org/gnu/gawk/gawk-5.3.1.tar.xz",
                      "ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz.sig",
                      "ftp.gnu.org/gnu/make/make-4.4.1.tar.gz"):
            self.assertIsNone(re.fullmatch(pattern, other))

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
                                   ("ftp.debian.org", "debian", "DEBIAN_MIRROR_ROOT"),
                                   ("security.debian.org", "debian-security", "DEBIAN_SECURITY_MIRROR_ROOT")):
            for suffix in ("dists/trixie/InRelease", "pool/main/a/audit/libaudit1_4.0.2-2+b2_amd64.deb"):
                with self.subTest(archive=archive, suffix=suffix):
                    self.assertEqual(native.destination(f"http://{host}/{archive}/{suffix}", MIRRORS, MANIFEST),
                        (MIRRORS[key] + MANIFEST[archive]["current"] + "/" + suffix, archive))

    def test_sonic_archive_alias_routes_pinned_libyang_sources_and_security(self):
        host = "debian-archive.trafficmanager.net"
        for filename in ("libyang_3.12.2-1.dsc", "libyang_3.12.2.orig.tar.xz",
                         "libyang_3.12.2-1.debian.tar.xz"):
            suffix = "pool/main/liby/libyang/" + filename
            with self.subTest(filename=filename):
                self.assertEqual(native.destination(f"http://{host}/debian/{suffix}", MIRRORS, MANIFEST),
                    (MIRRORS["DEBIAN_MIRROR_ROOT"] + MANIFEST["debian"]["current"] + "/" + suffix, "debian"))
        self.assertEqual(native.destination(f"http://{host}/debian-security/dists/trixie-security/InRelease",
                                           MIRRORS, MANIFEST),
            (MIRRORS["DEBIAN_SECURITY_MIRROR_ROOT"] + MANIFEST["debian-security"]["current"]
             + "/dists/trixie-security/InRelease", "debian-security"))
        unrelated = "http://debian-archive.trafficmanager.net.example.invalid/debian/pool/file.dsc"
        self.assertEqual(native.destination(unrelated, MIRRORS, MANIFEST), (unrelated, None))
        with self.assertRaises(ValueError):
            native.destination(f"http://{host}/debian/%2e%2e/private", MIRRORS, MANIFEST)

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

    def test_caller_xz_policy_and_mod_policy_preserve_native_routing(self):
        native.sys.path.insert(0, str(Path(__file__).resolve().parent))
        import github_release_transport as release
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed, metadata = root / "managed.cfg", root / "metadata.cfg"
            managed.write_text(release.downloader_policy("rewrite deb.example/(.*) https://private.example/$1\n", 32123))
            metadata.write_text(release.downloader_policy("rewrite metadata.example/(.*) https://private.example/$1\n", 32123))
            caller = Path("src/sonic-p4rt/source_mirrors.cfg").resolve()
            arguments = ["build", "--experimental_downloader_config=" + str(caller), "//p4rt_app:p4rt_deb"]
            command = native.bazel_command(arguments, managed, "/real-bazel", metadata)
            combined = Path(command[-3].split("=", 1)[1])
            policy = combined.read_text()
            previous = -1
            for line in caller.read_text().splitlines():
                if line.startswith("rewrite "):
                    position = policy.index(line)
                    self.assertGreater(position, previous)
                    previous = position
            self.assertIn("http://127.0.0.1:32123/_sonic_github_release/github.com/tukaani-project/xz/", policy)
            self.assertEqual(command[-2:], ["--disk_cache=", "--repository_cache="])
            self.assertEqual(combined.stat().st_mode & 0o777, 0o600)
            selected = native.bazel_command(["mod", "graph"], managed, "/real-bazel", metadata)
            self.assertEqual(selected[-3], "--experimental_downloader_config=" + str(metadata))
            self.assertNotIn("deb.example", metadata.read_text())
            args = ["run", "//:tool", "--", "--downloader_config=/nonexistent/target-argument"]
            selected = native.bazel_command(args, managed, "/real-bazel", metadata)
            self.assertEqual(selected[-2:], args[-2:])
            self.assertEqual(selected[selected.index("--") - 3], "--experimental_downloader_config=" + str(managed))

    def test_launcher_restores_action_path_and_drops_credential_before_real_bazel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "real-bazel"
            fake.write_text("#!/usr/bin/python3\nimport json,os,sys\nprint(json.dumps({'path':os.environ['PATH'],"
                            "'token': 'SONIC_GITHUB_RELEASE_TOKEN' in os.environ,'arguments':sys.argv[1:]}))\n")
            fake.chmod(0o755)
            environment = dict(os.environ, PATH="/temporary-launcher:/usr/bin:/bin",
                               SONIC_NATIVE_BAZEL_ORIGINAL_PATH="/usr/bin:/bin",
                               SONIC_GITHUB_RELEASE_TOKEN="fixture-token")
            result = native.subprocess.run([native.sys.executable, "-I", "-S", str(Path(native.__file__).resolve()),
                "bazel", str(root / "managed.cfg"), str(fake), "build", "//:target"],
                env=environment, text=True, capture_output=True, check=True)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["path"], "/usr/bin:/bin")
            self.assertFalse(receipt["token"])
            self.assertEqual(receipt["arguments"][-2:], ["--disk_cache=", "--repository_cache="])


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
            source_policy = "rewrite source.example.invalid/tool.tar.xz https://mirror.example.invalid/tool.tar.xz\n"
            (helpers / "source_mirrors.cfg").write_text(source_policy)
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
                configuration=mock.Mock(return_value="private fixture policy"),
                apt_mirror_map=lambda *args, **kwargs: {"public": "private"},
                write_config=lambda path, value: path.write_text(value),
                child_environment=lambda: {key: value for key, value in os.environ.items() if key not in MIRRORS})
            args = types.SimpleNamespace(helpers=helpers, artifacts=artifacts,
                private_dir=root / "private", phase="verification", check_artifacts=False,
                command=[native.sys.executable, "-c",
                    "import json,os,pathlib,shutil; "
                    "assert 'DEBIAN_MIRROR_ROOT' not in os.environ; "
                    "assert 'DEBIAN_SECURITY_MIRROR_ROOT' not in os.environ; "
                    "assert 'SONIC_GITHUB_RELEASE_TOKEN' not in os.environ; "
                    "assert '127.0.0.1' in os.environ['no_proxy'].split(','); "
                    "assert '127.0.0.1' in os.environ['NO_PROXY'].split(','); "
                    "assert shutil.which('bazel') == os.environ['BAZEL']; "
                    "assert os.environ['SONIC_BAZEL_REMOTE_ONLY_RC'] == " + repr(str(managed)) + "; "
                    "assert pathlib.Path(os.environ['SONIC_BAZEL_DOWNLOADER_CONFIG']).read_text().rstrip().endswith("
                    + repr(native.native_source_mirrors("private fixture policy")) + "); "
                    "assert 'fixture-release-token' not in pathlib.Path(os.environ['SONIC_BAZEL_DOWNLOADER_CONFIG']).read_text(); "
                    "assert pathlib.Path(os.environ['SONIC_BAZEL_METADATA_DOWNLOADER_CONFIG']).read_text().rstrip().endswith("
                    + repr(native.native_source_mirrors("private fixture policy")) + "); "
                    "assert os.environ['SONIC_BAZEL_METADATA_DOWNLOADER_CONFIG'] != os.environ['SONIC_BAZEL_DOWNLOADER_CONFIG']; "
                    "assert os.environ['BAZEL_CONTAINER_CACHE_DIR'] == ''; "
                    "assert os.environ['SONIC_NATIVE_DEBIAN_SNAPSHOT'] == '20260727T143429Z'; "
                    "assert os.environ['SONIC_NATIVE_DEBIAN_SECURITY_SNAPSHOT'] == '20260726T121236Z'"])
            child_check = args.command[-1]
            clean_environment = {key: value for key, value in os.environ.items()
                                 if key not in {"https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"}}
            clean_environment.update(http_proxy="http://existing-http.example.invalid:3128",
                                     HTTP_PROXY="http://existing-http.example.invalid:3128")
            proxy_url = "http://configured-proxy.example.invalid:3128"
            for phase in ("verification", "inputs"):
                args.phase = phase
                for inherited, expected in (({}, ""), ({"HTTPS_PROXY": proxy_url}, proxy_url),
                                            ({"https_proxy": proxy_url}, proxy_url),
                                            ({"https_proxy": "", "HTTPS_PROXY": proxy_url}, "")):
                    args.command[-1] = child_check
                    if phase == "inputs":
                        args.command[-1] += "; assert os.environ['HTTP_PROXY'] == 'http://existing-http.example.invalid:3128'"
                        for key in ("https_proxy", "HTTPS_PROXY"):
                            args.command[-1] += "; assert os.environ.get(" + repr(key) + ") == " + repr(inherited.get(key))
                        server_class = native.http.server.ThreadingHTTPServer
                        def loopback_only(address, handler):
                            self.assertEqual(address[0], "127.0.0.1", "Inputs must not start a Docker bridge proxy")
                            return server_class(address, handler)
                        bridge_guard = mock.patch.object(native.http.server, "ThreadingHTTPServer", side_effect=loopback_only)
                    else:
                        args.command[-1] += "; assert os.environ['HTTP_PROXY'].startswith('http://192.168.240.1:')"
                        args.command[-1] += "; assert os.environ['https_proxy'] == " + repr(expected)
                        args.command[-1] += "; assert os.environ['HTTPS_PROXY'] == " + repr(expected)
                        bridge_guard = contextlib.nullcontext()
                    with self.subTest(phase=phase, inherited=inherited), bridge_guard, \
                         mock.patch.dict(native.sys.modules, {"private_mirrors": private_mirrors,
                             "mirror_privacy": types.SimpleNamespace(Redactor=Redactor)}), \
                         mock.patch.dict(os.environ, {**clean_environment, **MIRRORS, **inherited,
                             "SONIC_GITHUB_RELEASE_TOKEN": "fixture-release-token",
                             "SONIC_BAZEL_REMOTE_ONLY_RC": str(managed)}, clear=True), \
                         mock.patch.object(native.subprocess, "check_output", return_value=native.REGISTRY_REVISION + "\n"), \
                         mock.patch.object(native.shutil, "which", return_value="/bin/true"), \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(native.run(args), 0)
                        self.assertEqual([call.kwargs["fallback"] for call in
                                          private_mirrors.configuration.call_args_list[-2:]],
                                         [phase != "inputs", False])
                        for call in private_mirrors.configuration.call_args_list[-2:]:
                            policy = call.args[0]
                            self.assertTrue(policy.startswith(source_policy))
                            self.assertIn(Path("tools/bazel/debian_mirrors.cfg").read_text().rstrip(), policy)
                            self.assertTrue(policy.endswith(
                                r"rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz "
                                "https://zlib.net/pigz/pigz-2.8.tar.gz\n"))
                        receipt = json.loads((artifacts / ("private-downloads-" + phase + ".json")).read_text())
                        self.assertEqual(receipt["debian_public_bazel_fallback"], phase != "inputs")
                        self.assertEqual(receipt["github_release_transport"],
                                         {"authenticated": True, "completed": 0, "failed": 0, "failure_categories": {}})
                        self.assertEqual(receipt["github_commit_archive_transport"],
                                         {"authenticated": False, "completed": 0, "failed": 0, "failure_categories": {}})
            self.assertEqual(list(args.private_dir.iterdir()), [])
            self.assertIn("https://remote.example.invalid/", seen)
            receipt = json.loads((artifacts / "private-downloads-verification.json").read_text())
            self.assertNotIn("example.invalid", json.dumps(receipt))
            self.assertFalse(receipt["local_bazel_caches"])


if __name__ == "__main__":
    unittest.main()
