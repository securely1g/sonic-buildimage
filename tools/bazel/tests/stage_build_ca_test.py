"""Exercise optional native-builder trust without Docker or changing host trust."""

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import jinja2


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/stage_build_ca.py"
SPEC = importlib.util.spec_from_file_location("stage_build_ca", SCRIPT)
stage_ca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage_ca)
TRUST_RC = "/usr/local/share/sonic-build-trust/bazel.bazelrc"
JAVA_OPTION = "startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts"
SYSTEM_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


class BuildCaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identities = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.identities.cleanup)
        directory = Path(cls.identities.name)
        certificates = []
        for index, is_ca in enumerate((True, True, False)):
            certificate = directory / (str(index) + ".crt")
            key = directory / (str(index) + ".key")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                 "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key),
                 "-out", str(certificate), "-days", "1", "-subj",
                 "/CN=SONiC build CA test " + str(index), "-addext",
                 "basicConstraints=critical,CA:" + ("TRUE" if is_ca else "FALSE")],
                check=True, capture_output=True,
            )
            certificates.append(certificate.read_bytes())
        cls.ca_certificates = certificates[:2]
        cls.leaf_certificate = certificates[2]
        cls.private_key = key.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.bundle = self.workspace / "explicit public CA bundle.pem"
        self.output = self.workspace / "sonic-slave-bookworm/build-ca-certificates"

    def stage(self, contents):
        self.bundle.write_bytes(contents)
        return stage_ca.stage(self.bundle, self.output)

    def staged(self):
        return {path.name: path.read_bytes() for path in self.output.iterdir()}

    def cli(self, bundle=None):
        command = [sys.executable, str(SCRIPT), "--output", str(self.output)]
        if bundle is not None:
            command += ["--bundle", str(bundle)]
        return subprocess.run(command, text=True, capture_output=True, timeout=10)

    def render(self, distro, digest=""):
        environment = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT))
        return environment.get_template("sonic-slave-" + distro + "/Dockerfile.j2").render(
            CONFIGURED_ARCH="amd64", DEFAULT_CONTAINER_REGISTRY="", MULTIARCH_QEMU_ENVIRON="n",
            CROSS_BUILD_ENVIRON="n", INCLUDE_FIPS="n", SONIC_BUILD_SLAVE_CA_SHA256=digest,
        )

    def make(self, bundle=None, distro="bookworm"):
        scripts = self.workspace / "scripts"
        scripts.mkdir(exist_ok=True)
        script = scripts / SCRIPT.name
        if not script.exists():
            script.symlink_to(SCRIPT)
        source = (ROOT / "Makefile.work").read_text()
        start = source.index("# Optional host trust belongs")
        end = source.index("# Generate the slave Dockerfile", start)
        makefile = self.workspace / "test.mk"
        makefile.write_text(
            "BLDENV=" + distro + "\nSLAVE_DIR=sonic-slave-" + distro + "\n" + source[start:end]
            + "\nall:\n\t@printf '%s\\n' '$(SONIC_BUILD_SLAVE_CA_SHA256)'\n"
        )
        env = {key: value for key, value in os.environ.items() if key not in {
            "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "SONIC_BUILD_SLAVE_CA_BUNDLE",
        }}
        command = ["make", "--no-print-directory", "-f", str(makefile)]
        if bundle is not None:
            command.append("SONIC_BUILD_SLAVE_CA_BUNDLE=" + str(bundle))
        return subprocess.run(command, cwd=self.workspace, env=env, text=True,
                              capture_output=True, timeout=10)

    def test_bundle_normalizes_unique_public_anchors_and_hashes_exact_input(self):
        contents = b"\n" + self.ca_certificates[1] + self.ca_certificates[0] * 2 + b"\n"
        digest = self.stage(contents)
        self.assertEqual(digest, hashlib.sha256(contents).hexdigest())
        self.assertEqual(set(self.staged().values()), set(self.ca_certificates))
        for path in self.output.iterdir():
            der = ssl.PEM_cert_to_DER_cert(path.read_text())
            self.assertEqual(path.name, hashlib.sha256(der).hexdigest() + ".crt")
            self.assertEqual(path.read_bytes().count(b"BEGIN CERTIFICATE"), 1)

    def test_invalid_material_is_rejected_before_creating_outputs(self):
        invalid = (b"", b"not a certificate", self.private_key,
                   self.ca_certificates[0] + self.private_key,
                   b"garbage\n" + self.ca_certificates[0],
                   b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
        for contents in invalid:
            with self.subTest(contents=contents[:25]), self.assertRaises(ValueError):
                self.stage(contents)
            self.assertFalse(self.output.exists())

    def test_leaf_certificate_is_rejected_alone_and_with_a_valid_ca(self):
        for contents in (self.leaf_certificate, self.ca_certificates[0] + self.leaf_certificate):
            with self.subTest(mixed=contents != self.leaf_certificate), self.assertRaises(ValueError):
                self.stage(contents)
            self.assertFalse(self.output.exists())

    def test_trailing_material_inside_one_pem_is_rejected_without_changing_outputs(self):
        ca_der = ssl.PEM_cert_to_DER_cert(self.ca_certificates[0].decode("ascii"))
        leaf_der = ssl.PEM_cert_to_DER_cert(self.leaf_certificate.decode("ascii"))
        for name, trailing in (("junk", b"\x00trailing non-certificate data"), ("leaf", leaf_der)):
            with self.subTest(trailing=name):
                self.output = self.workspace / ("trailing-" + name)
                contents = ssl.DER_cert_to_PEM_cert(ca_der + trailing).encode("ascii")
                with self.assertRaises(ValueError):
                    self.stage(contents)
                self.assertFalse(self.output.exists())
                self.stage(self.ca_certificates[0])
                previous = self.staged()
                with self.assertRaises(ValueError):
                    self.stage(contents)
                self.assertEqual(self.staged(), previous)

    def test_invalid_replacement_preserves_staged_trust(self):
        self.stage(self.ca_certificates[0])
        previous = self.staged()
        for contents in (b"invalid", self.ca_certificates[1] + self.private_key, self.leaf_certificate):
            with self.subTest(contents=contents[:25]), self.assertRaises(ValueError):
                self.stage(contents)
            self.assertEqual(self.staged(), previous)

    def test_oversized_bundle_is_rejected_before_writing(self):
        self.assertEqual(stage_ca.MAX_BUNDLE_BYTES, 8 * 1024 * 1024)
        contents = self.ca_certificates[0].ljust(stage_ca.MAX_BUNDLE_BYTES + 1, b"\n")
        with self.assertRaises(ValueError):
            self.stage(contents)
        self.assertFalse(self.output.exists())

    def test_missing_and_non_file_inputs_do_not_create_outputs(self):
        for path in (self.bundle, self.workspace):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                stage_ca.stage(path, self.output)
            self.assertFalse(self.output.exists())

    def test_digest_and_anchors_use_the_same_frozen_input(self):
        contents = self.ca_certificates[0]
        validate = stage_ca.certificates

        def replace_source_after_read(frozen):
            self.bundle.write_bytes(self.ca_certificates[1])
            return validate(frozen)

        with mock.patch.object(stage_ca, "certificates", side_effect=replace_source_after_read):
            digest = self.stage(contents)
        self.assertEqual(digest, hashlib.sha256(contents).hexdigest())
        self.assertEqual(set(self.staged().values()), {contents})
        self.assertEqual(self.bundle.read_bytes(), self.ca_certificates[1])

    def test_rotation_removes_old_anchors_and_repeated_staging_is_stable(self):
        first = self.stage(self.ca_certificates[0])
        old_names = set(self.staged())
        second = self.stage(self.ca_certificates[1])
        self.assertNotEqual(first, second)
        self.assertFalse(old_names & set(self.staged()))
        times = {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}
        self.assertEqual(self.stage(self.ca_certificates[1]), second)
        self.assertEqual({path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}, times)

    def test_stage_rejects_linked_output_or_parent_directories(self):
        outside = self.workspace / "outside"
        outside.mkdir()
        self.bundle.write_bytes(self.ca_certificates[0])
        for destination in (self.workspace / "linked", self.workspace / "linked" / "anchors"):
            link = self.workspace / "linked"
            link.symlink_to(outside, target_is_directory=True)
            try:
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    stage_ca.stage(self.bundle, destination)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                link.unlink()

    def test_stage_preserves_unrelated_files_and_linked_anchors(self):
        self.stage(self.ca_certificates[0])
        unrelated = self.output / "keep.txt"
        unrelated.write_text("keep")
        previous = self.staged()
        with self.assertRaises(ValueError):
            self.stage(self.ca_certificates[1])
        self.assertEqual(self.staged(), previous)
        unrelated.unlink()
        anchor = next(self.output.iterdir())
        anchor.unlink()
        anchor.symlink_to(self.bundle)
        with self.assertRaises(ValueError):
            stage_ca.stage(self.bundle, self.output)
        self.assertTrue(anchor.is_symlink())
        self.assertEqual(self.bundle.read_bytes(), self.ca_certificates[1])

    def test_public_output_modes_do_not_depend_on_host_umask(self):
        previous = os.umask(0o077)
        try:
            self.stage(self.ca_certificates[0])
        finally:
            os.umask(previous)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_clear_removes_only_managed_anchors_and_is_idempotent(self):
        self.stage(b"".join(self.ca_certificates))
        source = self.bundle.read_bytes()
        stage_ca.clear(self.output)
        self.assertFalse(self.output.exists() and list(self.output.iterdir()))
        stage_ca.clear(self.output)
        self.assertEqual(self.bundle.read_bytes(), source)

    def test_clear_absent_output_creates_nothing(self):
        stage_ca.clear(self.output)
        self.assertFalse(self.output.parent.exists())

    def test_clear_rejects_unrelated_files_and_symlinks_without_deleting(self):
        self.stage(self.ca_certificates[0])
        unrelated = self.output / "keep.txt"
        unrelated.write_text("keep")
        previous = self.staged()
        with self.assertRaises(ValueError):
            stage_ca.clear(self.output)
        self.assertEqual(self.staged(), previous)
        unrelated.unlink()
        anchor = next(self.output.iterdir())
        anchor.unlink()
        anchor.symlink_to(self.bundle)
        with self.assertRaises(ValueError):
            stage_ca.clear(self.output)
        self.assertTrue(anchor.is_symlink())
        link = self.workspace / "linked-output"
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(ValueError):
            stage_ca.clear(link)
        self.assertTrue(link.is_symlink())

    def test_cli_emits_only_digest_and_disable_is_silent(self):
        self.bundle.write_bytes(b"".join(self.ca_certificates))
        result = self.cli(self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, hashlib.sha256(self.bundle.read_bytes()).hexdigest() + "\n")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.output.exists() and list(self.output.iterdir()))

    def test_cli_validation_failure_has_no_digest_or_outputs(self):
        self.bundle.write_bytes(self.private_key)
        result = self.cli(self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("PRIVATE KEY", result.stderr)
        self.assertFalse(self.output.exists())

    def test_make_default_needs_no_bundle_and_creates_no_staging(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n")
        self.assertFalse(self.output.exists())

    def test_make_enable_rotate_disable_changes_identity_and_removes_stale_trust(self):
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                self.output = self.workspace / ("sonic-slave-" + distro) / "build-ca-certificates"
                identities = []
                for certificate in self.ca_certificates:
                    self.bundle.write_bytes(certificate)
                    result = self.make(self.bundle, distro)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    digest = hashlib.sha256(certificate).hexdigest()
                    self.assertEqual(result.stdout, digest + "\n")
                    self.assertEqual(set(self.staged().values()), {certificate})
                    identities.append(hashlib.sha1(self.render(distro, digest).encode()).hexdigest())
                self.assertNotEqual(*identities)
                result = self.make(distro=distro)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "\n")
                self.assertFalse(self.output.exists() and list(self.output.iterdir()))
                self.assertNotIn("build-ca-certificates", self.render(distro))

    def test_make_invalid_input_and_unsafe_clear_stop_before_rendering(self):
        self.stage(self.ca_certificates[0])
        previous = self.staged()
        self.bundle.write_text("invalid CA")
        result = self.make(self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.staged(), previous)
        (self.output / "keep.txt").write_text("keep")
        result = self.make()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual((self.output / "keep.txt").read_text(), "keep")

    def test_make_unsupported_distro_does_not_stage_selected_bundle(self):
        result = self.make(self.bundle, distro="bullseye")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n")
        self.assertFalse((self.workspace / "sonic-slave-bullseye").exists())

    def test_native_staging_does_not_change_shared_hooks_or_runtime_contexts(self):
        shared = self.workspace / "src/sonic-build-hooks/buildinfo"
        runtime = self.workspace / "dockers/docker-test"
        for directory in (shared, runtime):
            directory.mkdir(parents=True)
            (directory / "sentinel").write_bytes(b"unchanged runtime input")
        self.bundle.write_bytes(self.ca_certificates[0])
        result = self.make(self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.is_dir())
        for directory in (shared, runtime):
            self.assertEqual([path.name for path in directory.iterdir()], ["sentinel"])
            self.assertEqual((directory / "sentinel").read_bytes(), b"unchanged runtime input")

    def test_templates_install_system_trust_before_downloads_without_new_tls_bypasses(self):
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                default = self.render(distro)
                configured = self.render(distro, "a" * 64)
                install = 'COPY ["build-ca-certificates/", "/usr/local/share/ca-certificates/sonic-build/"]'
                self.assertNotIn(install, default)
                self.assertIn("Native builder CA bundle SHA256: " + "a" * 64, configured)
                self.assertLess(configured.index("install -y ca-certificates"), configured.index(install))
                self.assertLess(configured.index(install), configured.index("RUN update-ca-certificates"))
                self.assertLess(configured.index("RUN update-ca-certificates"),
                                configured.index("RUN pip3 install git+https://github.com/aristanetworks/swi-tools.git"))
                self.assertEqual(configured.count("Verify-Peer=false"), default.count("Verify-Peer=false"))
                self.assertNotIn("GIT_SSL_NO_VERIFY", configured)

    def test_client_bundle_overrides_are_opt_in_and_preserve_system_roots(self):
        clients = ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "PIP_CERT")
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                default = self.render(distro)
                configured = self.render(distro, "a" * 64)
                for variable in clients:
                    self.assertNotRegex(default, rf"\b{variable}(?:=|\s+){re.escape(SYSTEM_BUNDLE)}")
                    self.assertRegex(configured, rf"\b{variable}(?:=|\s+){re.escape(SYSTEM_BUNDLE)}")
                self.assertNotIn(TRUST_RC, default)
                self.assertNotIn(JAVA_OPTION, default)

    def test_generated_java_configuration_survives_the_make_system_rc_mount(self):
        slave_rc = (ROOT / "tools/bazel/slave.bazelrc").read_text()
        self.assertIn("try-import " + TRUST_RC, slave_rc)
        for distro in ("bookworm", "trixie"):
            with self.subTest(distro=distro):
                configured = self.render(distro, "a" * 64)
                logical_lines = configured.replace("\\\n", " ").splitlines()
                commands = [line[4:] for line in logical_lines
                            if line.startswith("RUN ") and TRUST_RC in line]
                self.assertEqual(len(commands), 1)
                execution = self.workspace / distro
                execution.mkdir()
                system_rc = execution / "bazel.bazelrc"
                trust_dir = execution / "trust"
                command = commands[0].replace("/usr/local/share/sonic-build-trust", str(trust_dir))
                command = command.replace("/etc/bazel.bazelrc", str(system_rc))
                result = subprocess.run(["sh", "-c", command], text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((trust_dir / "bazel.bazelrc").read_text(), JAVA_OPTION + "\n")
                self.assertIn("try-import " + str(trust_dir / "bazel.bazelrc"), system_rc.read_text())


if __name__ == "__main__":
    unittest.main()
