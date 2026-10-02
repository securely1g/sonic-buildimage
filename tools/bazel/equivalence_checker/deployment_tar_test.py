"""Exercise tar collection and payload/debug comparison without building any DEBs."""

import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collector
import extractor
from context import Context
from diagnostics import (
    ArtifactIdentifier,
    ArtifactIndex,
    ArtifactType,
    Codes,
    ComparableArtifact,
    DiagnosticSink,
    Modifier,
)
from tools import Bazel, Tool, Tools


def query_xml(*targets):
    import xml.etree.ElementTree as ET

    root = ET.Element("query")
    for name, tags in targets:
        rule = ET.SubElement(root, "rule", name=name, **{"class": "filegroup"})
        values = ET.SubElement(rule, "list", name="tags")
        for tag in tags:
            ET.SubElement(values, "string", value=tag)
    return ET.tostring(root, encoding="unicode")


class DeploymentTargetTests(unittest.TestCase):
    def targets(self, *targets):
        result = subprocess.CompletedProcess([], 0, stdout=query_xml(*targets))
        with mock.patch.object(Bazel, "run", return_value=result) as run:
            actual = Bazel().deployment_tar_targets("sysmgr")
        self.assertEqual(run.call_args.args[0], "query")
        self.assertIn("--output=xml", run.call_args.args)
        self.assertEqual(run.call_args.args[-1], 'attr(tags, "make-deb:", @sysmgr//:*)')
        return actual

    def test_explicit_runtime_debug_and_excluded_mappings(self):
        runtime = "@sysmgr//:sysmgr_runtime_equivalence"
        debug = "@sysmgr//:sysmgr_debug_pkg"
        compared, excluded = self.targets(
            (runtime, ["make-deb:sysmgr_1.0.0_amd64.deb"]),
            (debug, ["make-deb:sysmgr-dbg_1.0.0_amd64.deb", "no-elf-equivalence"]),
        )
        self.assertEqual(compared, {runtime: "sysmgr_1.0.0_amd64.deb"})
        self.assertEqual(excluded, {debug: "sysmgr-dbg_1.0.0_amd64.deb"})

    def test_invalid_or_ambiguous_mapping_fails(self):
        for tags in (
            ["make-deb:../sysmgr.deb"],
            ["make-deb:sysmgr.tar"],
            ["make-deb:"],
            ["make-deb:one.deb", "make-deb:two.deb"],
        ):
            with self.subTest(tags=tags), self.assertRaisesRegex(
                ValueError, "expected one"
            ):
                self.targets(("@sysmgr//:runtime", tags))

    def test_duplicate_make_package_fails(self):
        with self.assertRaisesRegex(ValueError, "Multiple tar targets"):
            self.targets(
                ("@sysmgr//:runtime", ["make-deb:sysmgr.deb"]),
                ("@sysmgr//:internal", ["make-deb:sysmgr.deb"]),
            )

    def test_collection_builds_only_mapped_tars_and_names_debug_from_make(self):
        bazel = mock.Mock(spec=Bazel)
        bazel.EXCLUDE_TAG = Bazel.EXCLUDE_TAG
        bazel.root_repo_names.return_value = {"sonic-sysmgr": "sysmgr"}
        runtime, debug = "@sysmgr//:runtime", "@sysmgr//:symbols"
        skipped = "@sysmgr//:excluded"
        bazel.deployment_tar_targets.return_value = (
            {runtime: "sysmgr_1.0.0_amd64.deb", debug: "sysmgr-dbg_1.0.0_amd64.deb"},
            {skipped: "excluded_1.0.0_amd64.deb"},
        )
        bazel.output_artifact.side_effect = lambda label, *_args, **_kwargs: Path(
            label.rsplit(":", 1)[1] + ".tar"
        )
        ctx = Context(
            DiagnosticSink(),
            ArtifactIndex(),
            bazel,
            mock.Mock(),
            True,
            "trixie",
            1,
            Path("unused"),
        )
        with mock.patch.object(
            collector.registry_lib,
            "discover_top_level_bazel_modules",
            return_value=[("sonic-sysmgr", Path("src/sonic-sysmgr"))],
        ):
            artifacts = collector._collect_deployment_tars(ctx)
        self.assertEqual(
            {a.makeVersion.name for a in artifacts},
            {"sysmgr_1.0.0_amd64.deb", "sysmgr-dbg_1.0.0_amd64.deb"},
        )
        self.assertEqual({a.type for a in artifacts}, {ArtifactType.TAR})
        symbols = next(
            a for a in artifacts if a.makeVersion.name.startswith("sysmgr-dbg")
        )
        self.assertEqual(symbols.identifier.name, "@sonic-sysmgr//:symbols")
        self.assertEqual(symbols.identifier.modifiers, frozenset({Modifier.DEBUG}))
        self.assertEqual(
            {call.args[0] for call in bazel.output_artifact.call_args_list},
            {runtime, debug},
        )
        self.assertEqual(
            [d.code for d in ctx.sink.diagnostics], [Codes.COLLECTION_EXCLUDED_BY_TAG]
        )


class DeploymentPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dpkg = mock.Mock(spec=Tool)

        # Represent already-built Make packages by their extracted payloads. The
        # stub exercises the extraction call contract without creating a DEB.
        def extract(option, source, destination):
            self.assertEqual(option, "-x")
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)

        self.dpkg.run.side_effect = extract
        readelf = mock.Mock(spec=Tool)
        readelf.run.side_effect = lambda _option, path: subprocess.CompletedProcess(
            [],
            0,
            stdout="Build ID: "
            + ("aa11" if b"make" in Path(path).read_bytes() else "bb22"),
        )
        self.ctx = Context(
            DiagnosticSink(),
            ArtifactIndex(),
            mock.Mock(spec=Bazel),
            Tools(readelf, mock.Mock(spec=Tool), mock.Mock(spec=Tool), self.dpkg),
            False,
            "trixie",
            1,
            self.root / "work",
        )

    def tree(self, name, files):
        root = self.root / name
        root.mkdir()
        for filename, contents in files.items():
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return root

    def archive(self, name, tree):
        archive = self.root / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tree, arcname=".")
        return archive

    def source(self, name, make, bazel):
        return ComparableArtifact(
            ArtifactIdentifier(name, None, frozenset()), bazel, make, ArtifactType.TAR
        )

    def test_payload_paths_links_differences_and_matching_debug_remain_compared(self):
        make = self.tree(
            "make-runtime",
            {
                "usr/bin/sysmgr": b"\x7fELFmake",
                "etc/sysmgr.conf": b"make",
                "etc/make-only": b"old",
            },
        )
        bazel = self.tree(
            "bazel-runtime",
            {
                "usr/bin/sysmgr": b"\x7fELFbazel",
                "etc/sysmgr.conf": b"bazel",
                "etc/bazel-only": b"new",
            },
        )
        for root in (make, bazel):
            (root / "usr/bin/alias").symlink_to("sysmgr")
        make_debug = self.tree(
            "make-debug", {"usr/lib/debug/.build-id/aa/11.debug": b"\x7fELFmake-debug"}
        )
        bazel_debug = self.tree(
            "bazel-debug",
            {"usr/lib/debug/.build-id/bb/22.debug": b"\x7fELFbazel-debug"},
        )
        runtime = self.source("@sysmgr//:runtime", make, self.archive("runtime", bazel))
        symbols = self.source(
            "@sysmgr//:symbols", make_debug, self.archive("symbols", bazel_debug)
        )
        artifacts = extractor.extract_all(self.ctx, [runtime, symbols])
        self.assertEqual(
            {(a.identifier.name, a.type) for a in artifacts},
            {
                ("/usr/bin/sysmgr", ArtifactType.ELF_EXECUTABLE),
                ("/usr/bin/sysmgr", ArtifactType.ELF_DEBUG_INFO),
                ("/usr/bin/alias", ArtifactType.LINK),
                ("/etc/sysmgr.conf", ArtifactType.FILE),
            },
        )
        debug = next(a for a in artifacts if a.type == ArtifactType.ELF_DEBUG_INFO)
        self.assertEqual(debug.identifier.source, runtime.identifier)
        self.assertEqual(debug.makeVersion.name, "11.debug")
        self.assertEqual(debug.bazelVersion.name, "22.debug")
        self.assertEqual(
            {(d.artifact.name, d.code) for d in self.ctx.sink.diagnostics},
            {
                ("/etc/make-only", Codes.EXTRACTION_MAKE_ONLY),
                ("/etc/bazel-only", Codes.EXTRACTION_BAZEL_ONLY),
            },
        )
        self.assertEqual(self.dpkg.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
