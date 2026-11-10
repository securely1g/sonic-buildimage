"""Pure data and toy-Make tests; no SONiC build goal is executed."""
from pathlib import Path
import hashlib
import subprocess
import tempfile
import unittest

from tools.bazel.ci import vs_input_inventory as inventory


ROOT = Path(__file__).resolve().parents[3]
SLAVE = (ROOT / "slave.mk").read_text()
METADATA = (ROOT / "tools/bazel/ci/vs_input_inventory.mk").read_text()
GUARD = SLAVE[SLAVE.index("# Inventory evaluates"):SLAVE.index("# Select bash")]
NO_REMAKE = METADATA[METADATA.index("# Explicit empty double-colon"):METADATA.index("VS_INPUT_ROWS :=")]
VALID = [
    "SONIC_VS_INPUT_INVENTORY=y", "SONIC_DEB_BUILD_POLICY=reuse-only",
    "SONIC_BAZEL_KERNEL_PREPARE=verify", "SONIC_BAZEL_VS_ARCHIVE_PROFILE=final23",
]


class InventoryMakeGuardTest(unittest.TestCase):
    def invoke(self, flags, goals, extra="", files=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "rules").mkdir()
        for name, body in (files or {}).items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(body)
        # Mock only the side effect. The mode guard itself is exact production text.
        text = GUARD + "\nifneq ($(SONIC_VS_INPUT_INVENTORY),y)\n$(shell touch setup-ran)\nendif\n" + extra
        text += "\n.PHONY: vs-input-inventory all\nvs-input-inventory:\n\t@printf inventory > observed\nall:\n\t@printf build > build-ran\n"
        (root / "Makefile").write_text(text)
        result = subprocess.run(
            ["make", "--no-builtin-rules", "--no-builtin-variables", "--no-print-directory", "-f", "Makefile", *flags, *goals],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
        )
        return root, result

    def test_inventory_expression_tracks_the_actual_installer(self):
        marker = "$(addprefix $(TARGET_PATH)/, $(SONIC_INSTALLERS)) : $(TARGET_PATH)/% : \\\n"
        self.assertEqual(SLAVE.count(marker), 1)
        expression = SLAVE.split(marker)[1].split("\n\n\t$(HEADER)", 1)[0]
        expression = expression.replace("$$*", "sonic-vs.bin").replace("$$", "$")
        self.assertIn("VS_INPUT_DIRECT = \\\n" + expression, METADATA)

    def test_kernel_scalar_whitespace_is_preserved(self):
        scalar = METADATA[METADATA.index("define vs_input_scalar_row"):].split("endef", 1)[0] + "endef\n"
        extra = "VS_INPUT_ROWS := scalar.rows\nempty :=\nspace := $(empty) $(empty)\nVALUE := $(space)$(space)stage1  stage2$(space)$(space)\n"
        extra += scalar + "$(call vs_input_scalar_row,kernel_config,ADDITIONAL_BUILD_PROFILES,$(VALUE))\n"
        root, result = self.invoke(VALID, ["vs-input-inventory"], extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "scalar.rows").read_text(), "kernel_config\tADDITIONAL_BUILD_PROFILES\t  stage1  stage2  \n")

    def test_inventory_mode_skips_setup_and_build(self):
        root, result = self.invoke(VALID, ["vs-input-inventory"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "observed").is_file())
        self.assertFalse((root / "setup-ran").exists())
        self.assertFalse((root / "build-ran").exists())

    def test_normal_mode_retains_setup(self):
        root, result = self.invoke([], ["all"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "setup-ran").is_file())
        self.assertTrue((root / "build-ran").is_file())

    def test_missing_inventory_flag_stops_before_setup(self):
        root, result = self.invoke(VALID[1:], ["vs-input-inventory"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "setup-ran").exists())
        self.assertFalse((root / "observed").exists())

    def test_other_and_mixed_goals_stop_before_setup(self):
        for goals in [["all"], ["vs-input-inventory", "all"], ["all", "vs-input-inventory"], []]:
            with self.subTest(goals=goals):
                root, result = self.invoke(VALID, goals)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "setup-ran").exists())
                self.assertFalse((root / "build-ran").exists())

    def test_invalid_mode_and_missing_policies_stop_before_setup(self):
        cases = [["SONIC_VS_INPUT_INVENTORY=maybe"], ["SONIC_VS_INPUT_INVENTORY=n"], ["SONIC_VS_INPUT_INVENTORY="]]
        cases += [[value for index, value in enumerate(VALID) if index != missing] for missing in (1, 2, 3)]
        for flags in cases:
            with self.subTest(flags=flags):
                root, result = self.invoke(flags, ["vs-input-inventory"])
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "setup-ran").exists())
                self.assertFalse((root / "observed").exists())

    def test_missing_optional_include_cannot_use_implicit_producer(self):
        extra = "-include rules/config.user\n%.user:\n\t@touch producer-ran $@\n" + NO_REMAKE
        root, result = self.invoke(VALID, ["vs-input-inventory"], extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "producer-ran").exists())
        self.assertFalse((root / "rules/config.user").exists())

    def test_explicit_include_producer_conflict_fails_closed(self):
        extra = "include rules/config.user\nrules/config.user: producer\n\t@touch $@\nproducer:\n\t@touch producer-ran\n" + NO_REMAKE
        root, result = self.invoke(VALID, ["vs-input-inventory"], extra, {"rules/config.user": "VALUE := retained\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "producer-ran").exists())
        self.assertFalse((root / "observed").exists())

    def test_existing_include_bytes_are_preserved(self):
        original = "VALUE := retained\n"
        extra = "include retained.mk\n%.mk:\n\t@touch producer-ran $@\n" + NO_REMAKE
        root, result = self.invoke(VALID, ["vs-input-inventory"], extra, {"retained.mk": original})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "retained.mk").read_text(), original)
        self.assertFalse((root / "producer-ran").exists())


class InventoryDataTest(unittest.TestCase):
    def rows(self):
        rows = {("format", "schema"): "1", ("profile", "architecture"): "amd64", ("profile", "platform"): "vs", ("profile", "distribution"): "trixie", ("root", "make_prerequisites"): "target/debs/trixie/app.deb-install"}
        rows.update({("category", key): "" for key in inventory.CATEGORIES})
        rows.update({("kernel_config", key): "" for key in inventory.KERNEL_SETTINGS})
        rows["category", "SONIC_MAKE_DEBS"] = "app.deb"
        rows["category", "SONIC_ONLINE_DEBS"] = "dep.deb"
        rows["node", "app.deb|DEPENDS"] = "dep.deb"
        rows["node", "dep.deb|URL"] = "https://user:secret@approved.example/package.deb?token=private"
        return rows

    def test_declared_graph_preserves_download_and_producer_distinction(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = inventory.inventory(self.rows(), Path(temporary))
        nodes = {node["name"]: node for node in result["selected_nodes"]}
        self.assertEqual(nodes["app.deb"]["kinds"], ["sonic-produced-deb"])
        self.assertEqual(nodes["dep.deb"]["kinds"], ["declared-download-deb"])
        self.assertEqual(nodes["dep.deb"]["declared_origins"], ["https://approved.example"])
        self.assertNotIn("secret", str(result))
        self.assertNotIn("token=private", str(result))
        self.assertTrue(all(node["availability"] == "not-inspected" for node in nodes.values()))

    def test_dependent_rootfs_graph_is_retained(self):
        rows = self.rows()
        rows["root", "rfs"] = "target/current.rfs.squashfs"
        rows["category", "SONIC_RFS_TARGETS"] = "current.rfs.squashfs parent.rfs.squashfs"
        rows["node", "current.rfs.squashfs|DEPENDENT_RFS"] = "parent.rfs.squashfs"
        rows["node", "parent.rfs.squashfs|DEBS_DEPENDS"] = "dep.deb"
        with tempfile.TemporaryDirectory() as temporary:
            result = inventory.inventory(rows, Path(temporary))
        nodes = {node["name"] for node in result["selected_nodes"]}
        self.assertTrue({"current.rfs.squashfs", "parent.rfs.squashfs", "dep.deb"} <= nodes)
        self.assertIn({"from": "current.rfs.squashfs", "field": "DEPENDENT_RFS", "to": "parent.rfs.squashfs"}, result["dependency_edges"])
        self.assertIn({"from": "parent.rfs.squashfs", "field": "DEBS_DEPENDS", "to": "dep.deb"}, result["dependency_edges"])

    def test_missing_category_or_duplicate_rows_rejected(self):
        rows = self.rows()
        raw = "".join(f"{kind}\t{key}\t{value}\n" for (kind, key), value in rows.items()).encode()
        self.assertEqual(inventory.parse_rows(raw), rows)
        with self.assertRaises(ValueError):
            inventory.parse_rows(raw + b"format\tschema\t1\n")
        del rows["category", "SONIC_ONLINE_DEBS"]
        with self.assertRaises(ValueError):
            inventory.parse_rows("".join(f"{kind}\t{key}\t{value}\n" for (kind, key), value in rows.items()).encode())

    def test_real_vs_parent_includes_preserve_declared_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "platform/vs").mkdir(parents=True)
            (root / "platform/template").mkdir()
            names = ["docker-syncd-trixie.mk", "docker-gbsyncd-trixie.mk"]
            declared = ["platform/vs/../template/" + name for name in names]
            for name in names:
                (root / "platform/template" / name).write_text(name + "\n")
            rows = self.rows()
            rows["include", "files"] = " ".join(declared)
            result = inventory.inventory(rows, root)
            self.assertEqual(result["parsed_make_sources"], [
                {"path": original, "normalized_path": "platform/template/" + name,
                 "sha256": hashlib.sha256((name + "\n").encode()).hexdigest()}
                for original, name in zip(declared, names)
            ])
            self.assertEqual(rows["include", "files"], " ".join(declared))

    def test_nested_parent_components_remain_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a/b/c").mkdir(parents=True)
            (root / "a/source.mk").write_text("retained")
            self.assertEqual(inventory.safe_file(root, "a/b/c/../../source.mk"), root / "a/source.mk")

    def test_parent_and_absolute_escapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            (root / "sub").mkdir(parents=True)
            (base / "outside.mk").write_text("outside")
            for name in ("../outside.mk", "sub/../../outside.mk", str(base / "outside.mk")):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    inventory.safe_file(root, name)

    def test_symlink_before_parent_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            (root / "inside").mkdir(parents=True)
            (base / "outside").mkdir()
            (root / "source.mk").write_text("inside")
            for target in (root / "inside", base / "outside"):
                (root / "link").symlink_to(target, target_is_directory=True)
                with self.subTest(target=target), self.assertRaises(ValueError):
                    inventory.safe_file(root, "link/../source.mk")
                (root / "link").unlink()

    def test_file_before_parent_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("not a directory")
            (root / "source.mk").write_text("retained")
            with self.assertRaises(ValueError):
                inventory.safe_file(root, "file/../source.mk")

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").write_text("original")
            (root / "link").symlink_to(root / "source")
            with self.assertRaises(ValueError):
                inventory.safe_file(root, "link")


if __name__ == "__main__":
    unittest.main()
