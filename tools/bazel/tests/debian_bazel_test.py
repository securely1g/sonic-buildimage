"""Check verified Debian runner downloads and per-user Bazel configuration."""

from contextlib import contextmanager
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import configure_debian_bazel as debian_bazel


class DebianBazelTest(unittest.TestCase):
    PINNED_SHA = "7c8f6684838354f3f6a9ee09a1bbc746a9c87a09a760c1e7aa9558019117946d"
    ORIGINAL_RC = "build --disk_cache=/cache\ncommon --repository_cache=/repo"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runner_temp = self.root / "runner temp's files"
        self.runner_temp.mkdir()
        self.trust_fixture = self.root / "cacerts"
        self.trust_fixture.write_bytes(b"test trust store")

    @contextmanager
    def environment(self, payload, digest, uid, runner_home):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == Path("/etc/ssl/certs/java/cacerts"):
                path = self.trust_fixture
            return original_stat(path, *args, **kwargs)

        with patch.object(debian_bazel.Path, "home", return_value=runner_home), \
                patch.object(debian_bazel.Path, "stat", autospec=True, side_effect=stat), \
                patch.object(debian_bazel, "urlopen", return_value=response), \
                patch.object(debian_bazel.os, "getuid", return_value=uid), \
                patch.dict(debian_bazel.os.environ, {"RUNNER_TEMP": str(self.runner_temp)}), \
                patch.object(debian_bazel.hashlib, "sha256") as sha256:
            # Exercise post-verification behavior without embedding the remote config.
            sha256.return_value.hexdigest.return_value = digest
            yield response, sha256

    def test_unverified_download_never_changes_configuration(self):
        runner_home = self.root / "runner-home"
        runner_home.mkdir()
        rc = runner_home / ".bazelrc"
        rc.write_text(self.ORIGINAL_RC)
        cases = (
            ("truncated", b"x" * 8918, self.PINNED_SHA),
            ("oversized", b"x" * 8920, self.PINNED_SHA),
            ("wrong hash", b"x" * 8919, "0" * 64),
        )
        for name, payload, digest in cases:
            with self.subTest(name=name), self.environment(payload, digest, 1000, runner_home) as mocks, \
                    patch.object(debian_bazel, "preserve_snapshot_filenames",
                                 wraps=debian_bazel.preserve_snapshot_filenames) as normalize:
                response, sha256 = mocks
                with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                    debian_bazel.configure()
                normalize.assert_not_called()
                response.read.assert_called_once_with(8920)
                if name == "wrong hash":
                    sha256.assert_called_once_with(payload)
                else:
                    sha256.assert_not_called()
            self.assertEqual(rc.read_text(), self.ORIGINAL_RC)
            self.assertEqual(list(self.runner_temp.iterdir()), [])

    def test_root_and_runner_settings_are_preserved_and_idempotent(self):
        payload = b"x" * 8919
        for uid in (0, 1000):
            with self.subTest(uid=uid):
                runner_home = self.root / f"user-{uid}"
                runner_home.mkdir()
                rc = runner_home / ".bazelrc"
                rc.write_text(self.ORIGINAL_RC)
                with self.environment(payload, self.PINNED_SHA, uid, runner_home) as mocks:
                    _, sha256 = mocks
                    debian_bazel.configure()
                    first = rc.read_text()
                    debian_bazel.configure()
                    self.assertEqual(sha256.call_args_list, [call(payload), call(payload)])
                self.assertEqual(rc.read_text(), first)
                self.assertTrue(first.startswith(self.ORIGINAL_RC + "\n"))
                lines = first.splitlines()
                self.assertEqual(sum("javax.net.ssl.trustStore=" in line for line in lines), 1)
                downloader = [line for line in lines if "downloader_config=" in line]
                self.assertEqual(len(downloader), 1)
                config = self.runner_temp / f"sonic-source-mirrors-{uid}.cfg"
                self.assertEqual(shlex.split(downloader[0]),
                                 ["common", "--experimental_downloader_config=" + str(config)])
                self.assertEqual(config.read_bytes()[-len(payload):], payload)
        self.assertEqual({path.name for path in self.runner_temp.iterdir()},
                         {"sonic-source-mirrors-0.cfg", "sonic-source-mirrors-1000.cfg"})

    def test_download_fallbacks_are_exact_and_keep_original_urls(self):
        original = b"# verified registry configuration\n"
        configured = debian_bazel.with_download_fallbacks(original)
        self.assertEqual(configured[-len(original):], original)
        companion = configured[:-len(original)].decode().splitlines()
        directives = [line.split(maxsplit=2) for line in companion if line and not line.startswith("#")]
        self.assertTrue(all(parts[0] == "rewrite" for parts in directives))
        expected = {source: f"https://snapshot.debian.org/file/{identifier}/{source.rsplit('/', 1)[-1]}"
                    for source, identifier in debian_bazel.SNAPSHOT_FALLBACKS.items()}
        for source, destination in expected.items():
            self.assertEqual(destination.rsplit("/", 1)[-1], source.rsplit("/", 1)[-1])
        expected.update(debian_bazel.DEBIAN_POOL_FALLBACKS)
        expected.update(debian_bazel.GNU_FALLBACKS)
        self.assertEqual(len(directives), 2 * (len(debian_bazel.SNAPSHOT_FALLBACKS)
                                               + len(debian_bazel.DEBIAN_POOL_FALLBACKS)
                                               + len(debian_bazel.GNU_FALLBACKS)))
        for source, destination in expected.items():
            with self.subTest(source=source):
                matches = [target for _, pattern, target in directives if re.fullmatch(pattern, source)]
                self.assertEqual(matches, [destination, "https://" + source])
                for changed in ("x" + source, source + "x",
                                source.replace(".", "X", 1)):
                    self.assertFalse(any(re.fullmatch(pattern, changed) for _, pattern, _ in directives))

    def test_pinned_snapshot_rewrites_retain_names_and_unrelated_bytes(self):
        prefix = r"snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/"
        filename = r"libcap-dev_2\.75-10\+deb13u1\+b1_amd64\.deb"
        source = "snapshot.debian.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap-dev_2.75-10+deb13u1+b1_amd64.deb"
        destination = "https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6"
        unrelated = (b"# Preserve spacing and CRLF  \r\n"
                     b"rewrite example\\.invalid/(.*) https://example.invalid/$1\n"
                     b"# https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6\n")
        original_fallback = f"rewrite {prefix}{filename} https://{source}\n".encode()
        original = unrelated + f"rewrite {prefix}{filename} {destination}\n".encode() + original_fallback
        normalized = debian_bazel.preserve_snapshot_filenames(original)
        self.assertEqual(normalized, unrelated + f"rewrite {prefix}({filename}) {destination}/$1\n".encode()
                         + original_fallback)
        old_pattern, new_pattern = prefix + filename, prefix + "(" + filename + ")"
        for candidate in (source, "x" + source, source + "x", source.replace("2.75", "2X75"),
                          source.replace("+deb13u1", "+deb13u2")):
            self.assertEqual(bool(re.fullmatch(old_pattern, candidate)),
                             bool(re.fullmatch(new_pattern, candidate)))
        match = re.fullmatch(new_pattern, source)
        self.assertEqual(match.group(1), source.rsplit("/", 1)[-1])
        self.assertEqual((destination + "/$1").replace("$1", match.group(1)),
                         destination + "/libcap-dev_2.75-10+deb13u1+b1_amd64.deb")


if __name__ == "__main__":
    unittest.main()
