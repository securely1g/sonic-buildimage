"""Keep legacy VS containers' libnl runtime and development DEBs together.

An OCI config-engine base can retain Debian's genuine package controls while
overlaying Bazel-built runtime files. Its recorded libnl version then differs
from the custom Make development DEBs. These tests follow the real Make
dependency expansion into both Dockerfile templates, and use tiny fixture DEBs
to exercise the exact-version failure and the complete replacement transaction.
They do not build project packages or model their maintainer scripts or ELF ABI.
"""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PACKAGES = {
    "libnl-3-200", "libnl-genl-3-200", "libnl-route-3-200",
    "libnl-nf-3-200", "libnl-cli-3-200",
}
DEVELOPMENT_PACKAGES = {"libnl-3-dev", "libnl-route-3-dev"}
LIBNL_PACKAGES = RUNTIME_PACKAGES | DEVELOPMENT_PACKAGES

# Minimal fixture controls preserve the real family's exact-version relations.
# Other system dependencies are deliberately outside this transaction test.
FIXTURE_DEPENDENCIES = {
    "libnl-3-200": [],
    "libnl-genl-3-200": ["libnl-3-200"],
    "libnl-route-3-200": ["libnl-3-200"],
    "libnl-nf-3-200": ["libnl-3-200", "libnl-route-3-200"],
    "libnl-cli-3-200": [
        "libnl-3-200", "libnl-genl-3-200", "libnl-nf-3-200", "libnl-route-3-200",
    ],
    "libnl-3-dev": ["libnl-3-200"],
    "libnl-route-3-dev": ["libnl-3-dev", "libnl-route-3-200"],
}


class LegacyLibnlInstallTest(unittest.TestCase):
    def make_selection(self, architecture):
        """Use the actual Syncd -> sairedis -> Common runtime dependency chain."""
        makefile = """
DBG_IMAGE_MARK = dbg
PLATFORM_PATH = platform/vs
TARGET_PATH = target
include rules/functions
include rules/libnl3.mk
include rules/libyang3.mk
include rules/swss-common.mk
include rules/p4lang.mk
include rules/dash-sai.mk
include rules/sairedis.mk
include platform/vs/syncd-vs.mk
include platform/vs/docker-syncd-vs.mk
include platform/vs/docker-gbsyncd-vs.mk
.PHONY: selected
selected:
	@printf '%s\\n' 'version=$(LIBNL3_VERSION_SONIC)'
	@printf '%s\\n' 'syncd=$(call expand,$($(DOCKER_SYNCD_BASE)_DEPENDS),RDEPENDS)'
	@printf '%s\\n' 'gbsyncd=$(call expand,$($(DOCKER_GBSYNCD_BASE)_DEPENDS),RDEPENDS)'
"""
        environment = dict(os.environ, LC_ALL="C")
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEFILES"):
            environment.pop(name, None)
        result = subprocess.run([
            "make", "--no-print-directory", "-f", "-", "selected",
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", "BLDENV=trixie",
            "CONFIGURED_PLATFORM=vs", f"CONFIGURED_ARCH={architecture}",
            "INCLUDE_VS_DASH_SAI=y", "ENABLE_PY2_MODULES=n",
        ], input=makefile, cwd=ROOT, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        # slave.mk removes repeated RDEPENDS with awk while preserving order.
        return values["version"], {
            image: list(dict.fromkeys(values[image].split()))
            for image in ("syncd", "gbsyncd")
        }

    def rendered_install(self, image, packages, version, architecture):
        """Check that the real template copies and installs the complete graph once."""
        template = Environment(
            loader=FileSystemLoader(str(ROOT)), undefined=StrictUndefined,
        ).get_template(f"platform/vs/docker-{image}-vs/Dockerfile.j2")
        rendered = template.render(**{
            "DOCKER_USERNAME": "test", "DOCKER_USERTAG": "latest",
            "LIBNL3_VERSION_SONIC": version, "CONFIGURED_ARCH": architecture,
            f"docker_{image}_vs_debs": " ".join(packages),
        })
        instructions = [shlex.split(line) for line in rendered.replace("\\\n", " ").splitlines()
                        if line.startswith(("RUN ", "COPY "))]
        installs = [line for line in instructions if line[:3] == ["RUN", "dpkg", "-i"]]
        self.assertEqual(len(installs), 1, "libnl development DEBs must not be installed early")
        transaction = installs[0]
        split = transaction.index("||")
        self.assertEqual(transaction[split:], ["||", "apt-get", "-y", "install", "-f", "--no-remove"])
        installed = [Path(path).name for path in transaction[3:split]]
        self.assertEqual(installed, packages)
        copied = [Path(path).name for line in instructions
                  if line[0] == "COPY" and line[-1] == "/debs/" for path in line[1:-1]]
        self.assertEqual(copied, packages)
        libnl = [name for name in installed if name.split("_", 1)[0] in LIBNL_PACKAGES]
        self.assertEqual({name.split("_", 1)[0] for name in libnl}, LIBNL_PACKAGES)
        self.assertTrue(all(name.endswith(f"_{version}_{architecture}.deb") for name in libnl))
        return libnl

    def test_real_make_graph_and_templates_install_complete_libnl_family_once(self):
        """Both legacy images must include route headers and all five runtime DEBs on both CPUs."""
        for architecture in ("amd64", "arm64"):
            version, selections = self.make_selection(architecture)
            for image, packages in selections.items():
                with self.subTest(architecture=architecture, image=image):
                    self.rendered_install(image, packages, version, architecture)

    def fixture_deb(self, directory, package, version, architecture):
        source = directory / f"{package}-{version}"
        control = source / "DEBIAN/control"
        control.parent.mkdir(parents=True)
        fields = [
            f"Package: {package}", f"Version: {version}", f"Architecture: {architecture}",
            "Maintainer: SONiC test fixture <nobody@example.invalid>",
            "Description: libnl transaction regression fixture",
        ]
        dependencies = FIXTURE_DEPENDENCIES[package]
        if dependencies:
            fields.append("Depends: " + ", ".join(f"{name} (= {version})" for name in dependencies))
        control.write_text("\n".join(fields) + "\n")
        payload = source / "usr/share/legacy-libnl-fixtures" / package
        payload.parent.mkdir(parents=True)
        payload.write_text(f"{package} {version}\n")
        output = directory / f"{package}_{version}_{architecture}.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(source), str(output)],
                       check=True, capture_output=True, text=True)
        return output

    @unittest.skipUnless(os.geteuid() == 0, "requires root for an isolated real dpkg database")
    def test_dpkg_rejects_dev_first_but_configures_combined_family(self):
        """Reproduce the observed baseline conflict, then install the rendered complete family."""
        architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
        version, selections = self.make_selection(architecture)
        baseline_version = "3.7.0-2"
        self.assertNotEqual(version, baseline_version, "fixture needs distinct baseline and SONiC versions")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            baseline = {name: self.fixture_deb(directory, name, baseline_version, architecture)
                        for name in sorted(RUNTIME_PACKAGES)}
            replacement = {name: self.fixture_deb(directory, name, version, architecture)
                           for name in sorted(LIBNL_PACKAGES)}
            for image, packages in selections.items():
                with self.subTest(image=image):
                    rootfs = directory / image
                    (rootfs / "var/lib/dpkg").mkdir(parents=True)
                    (rootfs / "var/lib/dpkg/status").touch()

                    def install(paths):
                        return subprocess.run([
                            "dpkg", f"--root={rootfs}", "--force-unsafe-io", "--install",
                            *map(str, paths),
                        ], env=dict(os.environ, LC_ALL="C"), text=True, capture_output=True)

                    seeded = install(baseline.values())
                    self.assertEqual(seeded.returncode, 0, seeded.stderr)
                    rejected = install(replacement[name] for name in sorted(DEVELOPMENT_PACKAGES))
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("dependency problems", rejected.stderr)
                    self.assertIn(f"(= {version})", rejected.stderr)
                    self.assertIn(baseline_version, rejected.stderr)

                    # Execute the libnl portion of the actual template's package order.
                    # Unrelated SONiC components are outside this small fixture.
                    libnl = self.rendered_install(image, packages, version, architecture)
                    installed = install(directory / name for name in libnl)
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    queried = subprocess.check_output([
                        "dpkg-query", f"--admindir={rootfs}/var/lib/dpkg", "--show",
                        "--showformat=${Package}\t${Version}\t${Status}\n",
                    ], text=True)
                    self.assertEqual(
                        {tuple(line.split("\t")) for line in queried.splitlines()},
                        {(name, version, "install ok installed") for name in LIBNL_PACKAGES},
                    )
                    for name in LIBNL_PACKAGES:
                        self.assertEqual((rootfs / "usr/share/legacy-libnl-fixtures" / name).read_text(),
                                         f"{name} {version}\n")
                    audit = subprocess.run(["dpkg", f"--root={rootfs}", "--audit"],
                                           text=True, capture_output=True)
                    self.assertEqual(audit.returncode, 0, audit.stderr)
                    self.assertEqual(audit.stdout, "")


if __name__ == "__main__":
    unittest.main()
