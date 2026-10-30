"""Check optional native-builder trust without Docker or disabled TLS checks."""

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jinja2


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/stage_build_ca.py"
SPEC = importlib.util.spec_from_file_location("stage_build_ca", SCRIPT)
stage_ca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage_ca)


class BuildCaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identities = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.identities.cleanup)
        directory = Path(cls.identities.name)
        cls.certificates = []
        for index in range(2):
            certificate = directory / (str(index) + ".crt")
            key = directory / (str(index) + ".key")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec",
                            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                            "-keyout", str(key), "-out", str(certificate), "-days", "1",
                            "-subj", "/CN=SONiC build CA test " + str(index),
                            "-addext", "basicConstraints=critical,CA:TRUE"],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            cls.certificates.append(certificate.read_bytes())
        cls.private_key = key.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.bundle = self.workspace / "host-trust.pem"
        self.output = self.workspace / "sonic-slave-bookworm/build-ca-certificates"

    def stage(self, contents):
        self.bundle.write_bytes(contents)
        return stage_ca.stage(self.bundle, self.output)

    def test_full_bundle_becomes_individual_public_anchors_and_digest_only_output(self):
        contents = self.certificates[0] + self.certificates[1] + self.certificates[0]
        digest = self.stage(contents)
        self.assertEqual(digest, hashlib.sha256(contents).hexdigest())
        paths = sorted(self.output.iterdir())
        self.assertEqual(len(paths), 2)
        self.assertEqual({path.read_bytes() for path in paths}, set(self.certificates))
        for path in paths:
            self.assertRegex(path.name, r"^[0-9a-f]{64}\.crt$")
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(path.read_bytes().count(b"BEGIN CERTIFICATE"), 1)
        result = subprocess.run([sys.executable, str(SCRIPT), "--bundle", str(self.bundle),
                                 "--output", str(self.output)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, digest + "\n")
        self.assertNotIn(str(self.bundle), result.stdout)

    def test_invalid_or_private_input_preserves_previously_staged_trust(self):
        self.stage(self.certificates[0])
        previous = {path.name: path.read_bytes() for path in self.output.iterdir()}
        for contents in (b"", b"not a certificate", self.certificates[0] + self.private_key,
                         b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"):
            with self.subTest(contents=contents[:25]), self.assertRaises(ValueError):
                self.stage(contents)
            self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, previous)

    def test_rotation_removes_old_anchors_and_changes_builder_identity(self):
        first = self.stage(self.certificates[0])
        old_names = {path.name for path in self.output.iterdir()}
        second = self.stage(self.certificates[1])
        self.assertNotEqual(first, second)
        self.assertFalse(old_names & {path.name for path in self.output.iterdir()})
        times = {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}
        self.assertEqual(self.stage(self.certificates[1]), second)
        self.assertEqual({path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}, times)
        for distro in ("bookworm", "trixie"):
            self.assertNotEqual(hashlib.sha1(self.render(distro, first).encode()).hexdigest(),
                                hashlib.sha1(self.render(distro, second).encode()).hexdigest())

    def test_staging_does_not_follow_links_or_remove_unrelated_files(self):
        self.stage(self.certificates[0])
        unrelated = self.output / "keep.txt"
        unrelated.write_text("keep")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.stage(self.certificates[1])
        self.assertEqual(unrelated.read_text(), "keep")
        unrelated.unlink()
        anchor = next(self.output.iterdir())
        anchor.unlink()
        anchor.symlink_to(self.bundle)
        with self.assertRaisesRegex(ValueError, "linked"):
            self.stage(self.certificates[1])

    def render(self, distro, digest=""):
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT))
        return environment.get_template("sonic-slave-" + distro + "/Dockerfile.j2").render(
            CONFIGURED_ARCH="amd64", DEFAULT_CONTAINER_REGISTRY="", MULTIARCH_QEMU_ENVIRON="n",
            CROSS_BUILD_ENVIRON="n", INCLUDE_FIPS="n", SONIC_BUILD_SLAVE_CA_SHA256=digest)

    def test_templates_add_system_trust_before_git_and_omit_it_by_default(self):
        digest = "a" * 64
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                default = self.render(distro)
                self.assertNotIn("build-ca-certificates", default)
                self.assertNotIn("Native builder CA bundle", default)
                self.assertNotIn("javax.net.ssl.trustStore", default)
                self.assertNotIn("/etc/bazel/sonic-build-ca.bazelrc", default)
                configured = self.render(distro, digest)
                install = 'COPY ["build-ca-certificates/", "/usr/local/share/ca-certificates/sonic-build/"]'
                self.assertIn("Native builder CA bundle SHA256: " + digest, configured)
                self.assertLess(configured.index("install -y ca-certificates"), configured.index(install))
                self.assertLess(configured.index(install), configured.index("RUN update-ca-certificates"))
                self.assertLess(configured.index("RUN update-ca-certificates"),
                                configured.index("RUN pip3 install git+https://github.com/aristanetworks/swi-tools.git"))
                # The existing bootstrap exception is unchanged; added trust
                # must not add another TLS-verification bypass.
                self.assertEqual(configured.count("Verify-Peer=false"), default.count("Verify-Peer=false"))
                self.assertNotIn("GIT_SSL_NO_VERIFY", configured)
                self.assertIn("startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts", configured)
                self.assertIn("> /etc/bazel/sonic-build-ca.bazelrc", configured)
                self.assertIn("try-import /etc/bazel/sonic-build-ca.bazelrc", configured)
                self.assertIn(">> /etc/bazel.bazelrc", configured)
                self.assertNotIn("--server_javabase", configured)
                self.assertLess(configured.index("RUN chmod 0755 /usr/local/bin/cargo"),
                                configured.index("startup --host_jvm_args=-Djavax.net.ssl.trustStore="))

    def test_shared_cache_config_preserves_optional_image_owned_trust(self):
        config = (ROOT / "tools/bazel/slave.bazelrc").read_text()
        self.assertIn("try-import /etc/bazel/sonic-build-ca.bazelrc\n", config)
        self.assertIn("common --repository_cache=/bazel_cache/repository_cache", config)
        self.assertIn("common --disk_cache=/bazel_cache/disk_cache", config)
        # The mounted file is shared with builders that have no custom CA.
        self.assertNotIn("javax.net.ssl.trustStore", config)
        self.assertNotRegex(config, r"(?m)^import /etc/bazel/sonic-build-ca\.bazelrc$")

    def make(self, bundle=None):
        scripts = self.workspace / "scripts"
        scripts.mkdir(exist_ok=True)
        script = scripts / SCRIPT.name
        if not script.exists():
            script.symlink_to(SCRIPT)
        source = (ROOT / "Makefile.work").read_text()
        start = source.index("# Optional host trust belongs")
        end = source.index("# Generate the slave Dockerfile", start)
        makefile = self.workspace / "test.mk"
        makefile.write_text("BLDENV=bookworm\nSLAVE_DIR=sonic-slave-bookworm\n" + source[start:end]
                            + "\nall:\n\t@printf '%s\\n' '$(SONIC_BUILD_SLAVE_CA_SHA256)'\n")
        env = {key: value for key, value in os.environ.items()
               if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "SONIC_BUILD_SLAVE_CA_BUNDLE"}}
        command = ["make", "--no-print-directory", "-f", str(makefile)]
        if bundle is not None:
            command.append("SONIC_BUILD_SLAVE_CA_BUNDLE=" + str(bundle))
        return subprocess.run(command, cwd=self.workspace, env=env, text=True, capture_output=True)

    def test_make_default_needs_no_bundle_and_invalid_selection_stops_rendering(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n")
        self.assertFalse(self.output.exists())
        self.bundle.write_text("invalid CA")
        result = self.make(self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to prepare SONIC_BUILD_SLAVE_CA_BUNDLE", result.stderr)
        self.assertEqual(result.stdout, "")
        self.bundle.write_bytes(b"".join(self.certificates))
        result = self.make(self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(self.bundle.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
