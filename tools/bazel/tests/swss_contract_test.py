#!/usr/bin/env python3
"""Current SWSS inventory must preserve complete programs and installed bytes."""

from pathlib import Path
import tempfile
import unittest

import swss_container_test as contract


class SwssContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        for name in ("dist", "debian", "cfgmgr"):
            (self.source / name).mkdir()
        programs = ["//cfgmgr:program" + str(number) for number in range(29)]
        (self.source / "dist/BUILD.bazel").write_text(
            "CPP_BINARIES = " + repr(programs) + "\n"
            "LUA_FILES = ['//cfgmgr:vs.lua']\n"
            "LUA_INSTALL_ALIASES = {'//cfgmgr:vs.lua': '//cfgmgr:mellanox.lua'}\n"
            "# Nonliteral build expressions must never be evaluated by this reader.\n"
            "OTHER = arbitrary_build_expression()\n")
        (self.source / "debian/swss.install").write_text("target/release/countersyncd usr/bin\n")
        (self.source / "cfgmgr/vs.lua").write_text("VS source is not installed\n")
        (self.source / "cfgmgr/mellanox.lua").write_text("Installed implementation\n")
        self.files = {"usr/bin/program" + str(number):
                      {"kind": "file", "mode": 0o755, "elf_machine": 62}
                      for number in range(29)}
        self.files["usr/bin/countersyncd"] = {"kind": "file", "mode": 0o755, "elf_machine": 62}
        self.files["usr/share/swss/vs.lua"] = {
            "kind": "file", "mode": 0o644,
            "sha256": contract.sha(self.source / "cfgmgr/mellanox.lua"),
        }

    def test_current_source_declarations_and_debian_rust_binary(self):
        self.assertEqual(len(contract.swss_contract(self.source, self.files)), 30)
        self.assertFalse((self.source / "bazel/production_sources.bzl").exists())

    def test_missing_program_is_rejected(self):
        del self.files["usr/bin/program0"]
        with self.assertRaisesRegex(ValueError, "differs from the source install contract"):
            contract.swss_contract(self.source, self.files)

    def test_unexpected_payload_is_rejected(self):
        self.files["usr/bin/extra"] = {"kind": "file", "mode": 0o755, "elf_machine": 62}
        with self.assertRaisesRegex(ValueError, "differs from the source install contract"):
            contract.swss_contract(self.source, self.files)

    def test_alias_must_install_the_selected_implementation(self):
        self.files["usr/share/swss/vs.lua"]["sha256"] = contract.sha(self.source / "cfgmgr/vs.lua")
        with self.assertRaisesRegex(ValueError, "installed data differs from source"):
            contract.swss_contract(self.source, self.files)

    def test_missing_alias_declaration_is_rejected(self):
        build = self.source / "dist/BUILD.bazel"
        build.write_text(build.read_text().replace(
            "LUA_INSTALL_ALIASES = {'//cfgmgr:vs.lua': '//cfgmgr:mellanox.lua'}\n", ""))
        with self.assertRaisesRegex(ValueError, "missing SWSS source install declarations"):
            contract.swss_contract(self.source, self.files)


if __name__ == "__main__":
    unittest.main()
