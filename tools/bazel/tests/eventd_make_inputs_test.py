#!/usr/bin/env python3
"""Check eventd's Docker context inputs without building packages or images."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[3]


class EventdMakeInputsTest(unittest.TestCase):
    def test_runtime_closure_is_installed_even_when_base_only_overlays_source_tars(self):
        """Legacy eventd needs real dependency DEBs; a base's source TAR is not dpkg state."""
        makefile = """
DBG_IMAGE_MARK = dbg
DOCKERS_PATH = dockers
include rules/functions
# Match slave.mk's rules/*.mk order: derive eventd packages before Common is defined.
include $(sort rules/libnl3.mk rules/libyang3.mk rules/swss-common.mk rules/eventd.mk \\
               rules/docker-config-engine-trixie.mk rules/docker-eventd.mk)
.PHONY: selected
selected:
	@printf '%s\\n' 'eventd=$(call expand,$($(DOCKER_EVENTD)_DEPENDS),RDEPENDS)'
	@printf '%s\\n' 'plugin=$(call expand,$(SONIC_RSYSLOG_PLUGIN),RDEPENDS)'
"""
        runtime_names = {
            "libswsscommon", "libyang3", "libnl-3-200", "libnl-genl-3-200",
            "libnl-route-3-200", "libnl-nf-3-200", "libnl-cli-3-200",
        }
        template = Environment(
            loader=FileSystemLoader(str(ROOT)), undefined=StrictUndefined,
        ).get_template("dockers/docker-eventd/Dockerfile.j2")
        environment = dict(os.environ, LC_ALL="C")
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEFILES"):
            environment.pop(name, None)
        for architecture in ("amd64", "arm64"):
            for bazel in ("n", "y"):
                with self.subTest(architecture=architecture, bazel=bazel):
                    result = subprocess.run([
                        "make", "--no-print-directory", "-f", "-", "selected",
                        "BLDENV=trixie", "CONFIGURED_PLATFORM=vs", "INCLUDE_FIPS=y",
                        f"CONFIGURED_ARCH={architecture}",
                        f"BUILD_WITH_BAZEL_WHEN_AVAILABLE={bazel}",
                    ], input=makefile, cwd=ROOT, env=environment, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    # Match slave.mk's order-preserving deduplication of expanded RDEPENDS.
                    selected = {key: list(dict.fromkeys(value.split())) for key, value in
                                (line.split("=", 1) for line in result.stdout.splitlines())}
                    packages = selected["eventd"]
                    self.assertEqual(len(packages), 8)
                    self.assertEqual({p.split("_", 1)[0] for p in packages},
                                     runtime_names | {"sonic-eventd"})
                    self.assertTrue(all(p.endswith(f"_{architecture}.deb") for p in packages))
                    self.assertFalse(any("-dev_" in p for p in packages))
                    self.assertEqual(set(selected["plugin"]), {
                        p.replace("sonic-eventd_", "sonic-rsyslog-plugin_") for p in packages
                    }, "rsyslog plugin must select the same Common runtime closure")
                    rendered = template.render(
                        DOCKER_USERNAME="test", DOCKER_USERTAG="latest",
                        docker_eventd_debs=" ".join(packages),
                    )
                    instructions = [shlex.split(line) for line in
                                    rendered.replace("\\\n", " ").splitlines()
                                    if line.startswith(("RUN ", "COPY "))]
                    installs = [line for line in instructions
                                if line[:3] == ["RUN", "dpkg", "-i"]]
                    self.assertEqual(len(installs), 1)
                    transaction = installs[0]
                    fallback = transaction.index("||")
                    self.assertEqual(transaction[3:fallback], [f"/debs/{p}" for p in packages])
                    self.assertEqual(transaction[fallback:],
                                     ["||", "apt-get", "-y", "install", "-f", "--no-remove"])
                    copies = [line for line in instructions
                              if line[0] == "COPY" and line[-1] == "/debs/"]
                    self.assertEqual(len(copies), 1)
                    self.assertEqual([Path(p).name for p in copies[0][1:-1]], packages)

    def test_template_is_staged_without_building_the_legacy_config_engine(self):
        # Use the production copy recipe and Docker prerequisites. A source-built
        # config-engine base does not populate Make's shared files directory.
        slave = (ROOT / "slave.mk").read_text()
        copy_start = "$(addprefix $(FILES_PATH)/, $(SONIC_COPY_FILES)) :"
        copy_rule = copy_start + slave.split(copy_start, 1)[1].split(
            "\nSONIC_TARGET_LIST", 1
        )[0]
        docker_start = "$(addprefix $(TARGET_PATH)/, $(DOCKER_IMAGES)) :"
        docker_rule = docker_start + slave.split(docker_start, 1)[1].split(
            "\n\t$(HEADER)", 1
        )[0]

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            files = target / "files"
            files.mkdir(parents=True)
            template = files / "rsyslog_plugin.conf.j2"
            self.assertFalse(template.exists())
            makefile = f"""
.SECONDEXPANSION:
TARGET_PATH := {target}
FILES_PATH := $(TARGET_PATH)/files
DOCKERS_PATH := dockers
DOCKER_CONFIG_ENGINE_TRIXIE := docker-config-engine-trixie.gz
include rules/scripts.mk
include rules/docker-eventd.mk
DOCKER_IMAGES := $(DOCKER_EVENTD)
$(DOCKER_EVENTD)_FILES_PATH := $(FILES_PATH)
.PHONY: .platform docker-start $(TARGET_PATH)/$(DOCKER_CONFIG_ENGINE_TRIXIE)-load
.platform docker-start $(TARGET_PATH)/$(DOCKER_CONFIG_ENGINE_TRIXIE)-load:
\t@:
{copy_rule}
{docker_rule}
\t@test -s $(FILES_PATH)/rsyslog_plugin.conf.j2
\t@touch $@
"""
            result = subprocess.run(
                ["make", "--no-print-directory", "-f", "-", str(target / "docker-eventd.gz")],
                input=makefile, cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(template.read_bytes(),
                             (ROOT / "files/build_templates/rsyslog_plugin.conf.j2").read_bytes())
            self.assertTrue((target / "docker-eventd.gz").exists())


if __name__ == "__main__":
    unittest.main()
