"""Keep ordinary SWSS next-hop classes distinct from the separate libFIB ABI."""

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.swss_native_closure import FIB
from tools.bazel.ci import swss_native_closure as closure


class FibOwnershipTest(unittest.TestCase):
    def test_swss_next_hop_types_are_not_a_libfib_dependency(self):
        for value in ("NextHopGroupKey", "FdbDest::NEXTHOPGROUP", '"nexthopgroupkey.h"'):
            with self.subTest(value=value):
                self.assertIsNone(FIB.search(value))

    def test_library_owner_header_and_c_abi_require_review(self):
        for value in ("libnexthopgroup.so.0", "-lnexthopgroup", "//src/libraries/sonic-fib:nexthopgroup",
                      "nexthopgroup/c_nexthopgroupfull.h", "fib_frr_set_log_level(3)",
                      "nexthopgroupfull_json_from_c_nhg_multi(group)", "C_NextHopGroupFull"):
            with self.subTest(value=value):
                self.assertIsNotNone(FIB.search(value))


class LibteamOwnerLinkTest(unittest.TestCase):
    def graph(self, directory, *, adapter=None, wrong_owner=False, unrelated=False,
              omitted_adapter=False, producer_cycle=False, set_cycle=False, duplicate=False):
        graph = {"targets": [], "actions": [], "artifacts": [], "pathFragments": [], "depSetOfFiles": []}
        def target(label):
            identity = str(len(graph["targets"]) + 1)
            graph["targets"].append({"id": identity, "label": label})
            return identity
        def artifact(path):
            identity = str(len(graph["artifacts"]) + 1)
            graph["artifacts"].append({"id": identity, "pathFragmentId": identity})
            graph["pathFragments"].append({"id": identity, "label": path})
            return identity
        def inputs(ids):
            identity = str(len(graph["depSetOfFiles"]) + 1)
            graph["depSetOfFiles"].append({"id": identity, "directArtifactIds": ids})
            return [identity]
        libraries = {}
        for name, library in (("libteam", "libteam.so.5.6.1"), ("libteamdctl", "libteamdctl.so.0.1.5")):
            source = artifact("bazel-out/k8-opt/bin/external/libteam+/" + library)
            owner = target(("@@wrong-owner+//:" if wrong_owner else "@@libteam+//:") + name + "_shared")
            graph["actions"].append({"mnemonic": "CppLink", "targetId": owner, "outputIds": [source]})
            selected = source
            if adapter:
                if adapter == "interface":
                    interface = artifact("bazel-out/k8-opt/bin/external/libteam+/" + library + ".ifso")
                    # Bazel's native link action owns both outputs; there is
                    # no invented generic InterfaceLibrary adapter.
                    graph["actions"][-1]["outputIds"].append(interface)
                    selected = interface
                selected = artifact(directory + "/" + library)
                if not omitted_adapter:
                    previous = interface if adapter == "interface" else source
                    graph["actions"].append({"mnemonic": "SolibSymlink", "targetId": owner,
                                             "outputIds": [selected], "inputDepSetIds": inputs([selected if producer_cycle else previous])})
            libraries[library] = selected
        for name in ("teamsyncd", "tlm_teamd"):
            selected = list(libraries.values()) if name == "teamsyncd" else [libraries["libteamdctl.so.0.1.5"]]
            if unrelated:
                # The correct producers exist elsewhere in the graph, but none
                # of their outputs are actual inputs to this executable link.
                selected = []
            link = {"mnemonic": "CppLink", "targetId": target("@@sonic-swss+//:" + name),
                    "inputDepSetIds": inputs(selected), "arguments": ["-L" + directory, "-l:libteamdctl.so.0.1.5"]}
            if name == "teamsyncd": link["arguments"].append("-l:libteam.so.5.6.1")
            graph["actions"].append(link)
        if set_cycle:
            row = graph["depSetOfFiles"][-1]
            row["transitiveDepSetIds"] = [row["id"]]
        if duplicate:
            graph["actions"].append(dict(graph["actions"][0]))
        return graph

    def check_graph(self, graph):
        """Exercise real link checks, stopping before ELF/GDB/archive execution."""
        class LinksAccepted(Exception): pass
        programs = ["usr/bin/teamsyncd", "usr/bin/tlm_teamd"]
        with tempfile.TemporaryDirectory(prefix="swss-owner-link-test-") as directory:
            root = Path(directory); source = root / "source"; source.mkdir()
            (source / "consumer.cpp").write_text("int main() { return 0; }\n")
            graph_path = root / "links.json"; graph_path.write_text(json.dumps(graph))
            with mock.patch.object(closure.package_contract, "read_policy", return_value={"install_inventory": {}}), \
                 mock.patch.object(closure.package_contract, "validate_install_inventory", return_value=programs), \
                 mock.patch.object(closure.artifacts, "payload", return_value={}), \
                 mock.patch.object(closure.artifacts, "debug_archives", return_value={"debug_pairs": [{"path": p} for p in programs]}), \
                 mock.patch.object(closure.tarfile, "open", side_effect=LinksAccepted):
                try:
                    closure.verify(source, root / "runtime.tar", root / "symbols.tar", root / "policy.json", graph_path)
                except LinksAccepted: return
                self.fail("link validation did not reach the archive boundary")

    def test_shared_owner_and_response_file_links_are_accepted(self):
        for adapter, directory in ((None, "bazel-out/k8-opt/bin/external/libteam+"),
                                   ("solib", "bazel-out/k8-opt/bin/_solib_k8/owner"),
                                   ("interface", "bazel-out/k8-opt/bin/_solib_k8/owner")):
            for response in (False, True):
                for split in (False, True):
                    with self.subTest(adapter=adapter, response=response, split=split):
                        graph = self.graph(directory, adapter=adapter)
                        for action in graph["actions"][-2:]:
                            if split: action["arguments"][:1] = ["-L", directory]
                            if response: action.update(paramFiles=[{"arguments": action.pop("arguments")}], arguments=[])
                        self.check_graph(graph)

    def test_wrong_and_unrelated_present_owners_are_rejected(self):
        for variant in ("wrong_owner", "unrelated"):
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, "shared (libteam owner|library)"):
                self.check_graph(self.graph("bazel-out/k8-opt/bin/external/libteam+", **{variant: True}))

    def test_stock_and_similarly_named_providers_are_rejected(self):
        for directory in ("/usr/lib", "bazel-out/bin/external/libteam-fake+"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.check_graph(self.graph(directory, adapter="solib", wrong_owner=True))
        graph = self.graph("bazel-out/k8-opt/bin/external/libteam+")
        graph["actions"][-2]["arguments"].append("-Lsomething/libteam-dev")
        with self.assertRaisesRegex(ValueError, "stock Debian libteam"):
            self.check_graph(graph)

    def test_shared_directory_does_not_replace_required_library_check(self):
        graph = self.graph("bazel-out/k8-opt/bin/external/libteam+")
        graph["actions"][-2]["arguments"].remove("-l:libteam.so.5.6.1")
        with self.assertRaisesRegex(ValueError, "owner library outputs"):
            self.check_graph(graph)

    def test_cpp_link_only_graph_cannot_prove_solib_ownership(self):
        graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib", omitted_adapter=True)
        with self.assertRaisesRegex(ValueError, "shared libteam owner"):
            self.check_graph(graph)

    def test_graph_cycles_and_ambiguous_producers_are_rejected(self):
        for variant in ("producer_cycle", "set_cycle", "duplicate"):
            with self.subTest(variant=variant), self.assertRaisesRegex(ValueError, "cyclic|ambiguous"):
                self.check_graph(self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib", **{variant: True}))

    def test_owner_library_must_be_in_the_selected_search_directory(self):
        graph = self.graph("bazel-out/k8-opt/bin/external/libteam+")
        graph["actions"][-2]["arguments"][0] = "-Lelsewhere"
        with self.assertRaisesRegex(ValueError, "unique declared shared library"):
            self.check_graph(graph)

    def test_absolute_search_directory_is_not_silently_skipped(self):
        graph = self.graph("bazel-out/k8-opt/bin/external/libteam+")
        graph["actions"][-2]["arguments"].insert(0, "-L/usr/lib")
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            self.check_graph(graph)

    def test_earlier_wrong_owner_search_directory_is_not_ignored(self):
        graph = self.graph("bazel-out/k8-opt/bin/external/libteam+")
        graph["pathFragments"].append({"id": "99", "label": "bazel-out/earlier/libteam.so.5.6.1"})
        graph["artifacts"].append({"id": "99", "pathFragmentId": "99"})
        graph["targets"].append({"id": "99", "label": "@@wrong-owner+//:libteam_shared"})
        graph["actions"].insert(0, {"mnemonic": "CppLink", "targetId": "99", "outputIds": ["99"]})
        link = graph["actions"][-2]
        link["arguments"].insert(0, "-Lbazel-out/earlier")
        row = next(row for row in graph["depSetOfFiles"] if row["id"] == link["inputDepSetIds"][0])
        row["directArtifactIds"].append("99")
        with self.assertRaisesRegex(ValueError, "shared libteam owner"):
            self.check_graph(graph)

    def test_wrong_link_producer_cannot_launder_an_owner_dependency(self):
        graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
        graph["targets"].append({"id": "99", "label": "@@wrong-owner+//:libteam_shared"})
        adapter = next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")
        adapter.update(mnemonic="CppLink", targetId="99")
        with self.assertRaisesRegex(ValueError, "shared libteam owner"):
            self.check_graph(graph)

    def test_arbitrary_generator_cannot_launder_an_owner_dependency(self):
        for mnemonic in ("Genrule", "CopyFile", "InterfaceLibrary", "Symlink"):
            with self.subTest(mnemonic=mnemonic):
                graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
                adapter = next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")
                adapter["mnemonic"] = mnemonic
                with self.assertRaisesRegex(ValueError, "shared libteam owner"):
                    self.check_graph(graph)

    def test_solib_adapter_cannot_select_an_unrelated_owner_among_inputs(self):
        graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
        adapter = next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")
        row = next(row for row in graph["depSetOfFiles"] if row["id"] == adapter["inputDepSetIds"][0])
        row["directArtifactIds"].append("3")
        with self.assertRaisesRegex(ValueError, "ambiguous solib"):
            self.check_graph(graph)

    def test_owner_cooutput_is_not_enough_for_unrelated_adapter_input(self):
        graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="interface")
        fragment = next(row for row in graph["pathFragments"] if row["label"].endswith("libteam.so.5.6.1.ifso"))
        fragment["label"] = fragment["label"].replace("libteam.so.5.6.1.ifso", "unrelated.dat")
        with self.assertRaisesRegex(ValueError, "not the required library"):
            self.check_graph(graph)

    def test_filtered_query_retains_only_supported_producer_relationships(self):
        import copy
        import re
        expression = 'mnemonic("^(CppLink|SolibSymlink)$", deps(@sonic_swss//dist:swss_pkg))'
        workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/bazel-swss-native.yml").read_text()
        self.assertEqual(workflow.count("'" + expression + "'"), 1)
        self.assertEqual(workflow.count('"$BAZEL" aquery --include_param_files --output=jsonproto'), 1)
        supported = re.compile(r"^(CppLink|SolibSymlink)$")
        for adapter in (None, "solib", "interface"):
            directory = "bazel-out/k8-opt/bin/" + ("_solib_k8/owner" if adapter else "external/libteam+")
            graph = self.graph(directory, adapter=adapter)
            graph["actions"].append({"mnemonic": "CppCompile", "targetId": "1", "outputIds": []})
            selected = copy.deepcopy(graph)
            selected["actions"] = [action for action in graph["actions"] if supported.fullmatch(action["mnemonic"])]
            self.assertEqual(selected["artifacts"], graph["artifacts"])
            self.assertEqual(selected["depSetOfFiles"], graph["depSetOfFiles"])
            self.check_graph(selected)
        for mnemonic in ("Genrule", "CopyFile", "CppLinkExtra", "OtherSolibSymlink"):
            graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
            next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")["mnemonic"] = mnemonic
            graph["actions"] = [action for action in graph["actions"] if supported.fullmatch(action["mnemonic"])]
            with self.assertRaisesRegex(ValueError, "shared libteam owner"):
                self.check_graph(graph)

    def test_unrelated_duplicate_outputs_do_not_reject_selected_libraries(self):
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
                graph["artifacts"].append({"id": "99", "pathFragmentId": "99"})
                graph["pathFragments"].append({"id": "99", "label": "bazel-out/k8-opt/bin/other/helper.so"})
                graph["targets"].append({"id": "99", "label": "@@other+//:helper"})
                graph["actions"].extend([
                    {"mnemonic": "CppLink", "targetId": "99", "outputIds": ["99"], "actionKey": "a" * 64},
                    {"mnemonic": "CppLink", "targetId": "99", "outputIds": ["99"], "actionKey": "b" * 64},
                ])
                if consumed:
                    graph["depSetOfFiles"][-2]["directArtifactIds"].append("99")
                self.check_graph(graph)

    def test_equivalent_shareable_solib_edges_preserve_unique_leaf_owner(self):
        for adapter in ("solib", "interface"):
            with self.subTest(adapter=adapter):
                graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter=adapter)
                original = next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")
                original["actionKey"] = "a" * 64
                original["configurationId"] = "1"
                other = dict(original, targetId=graph["actions"][-2]["targetId"], configurationId="2")
                graph["actions"].append(other)
                self.check_graph(graph)
                graph["actions"].append(dict(graph["actions"][0]))
                with self.assertRaisesRegex(ValueError, "ambiguous selected library producer"):
                    self.check_graph(graph)

    def test_conflicting_or_keyless_selected_adapters_are_rejected_safely(self):
        for variant in ("keyless", "different-key", "different-input", "multiple-inputs", "mixed", "too-many", "private-label"):
            with self.subTest(variant=variant):
                graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="solib")
                original = next(action for action in graph["actions"] if action["mnemonic"] == "SolibSymlink")
                original["actionKey"] = "a" * 64
                other = dict(original)
                if variant == "keyless": other.pop("actionKey")
                elif variant == "different-key": other["actionKey"] = "b" * 64
                elif variant in ("different-input", "multiple-inputs"):
                    graph["depSetOfFiles"].append({"id": "99", "directArtifactIds": ["3"] if variant == "different-input" else ["1", "3"]})
                    other["inputDepSetIds"] = ["99"]
                elif variant == "private-label":
                    graph["targets"].append({"id": "99", "label": "@@private-token-secret//private:target"})
                    other.update(targetId="99", actionKey="secret-key", configurationId="private-config")
                else: other["mnemonic"] = "CppLink"
                graph["actions"].extend([other] * (7 if variant == "too-many" else 1))
                with self.assertRaisesRegex(ValueError, "ambiguous selected library producer") as caught:
                    self.check_graph(graph)
                message = str(caught.exception)
                self.assertIn('"library": "libteam.so.5.6.1"', message)
                self.assertNotIn("private-token-secret", message)
                self.assertNotIn("secret-key", message)
                self.assertNotIn("private-config", message)
                self.assertLess(len(message), 4096)
                if variant == "too-many": self.assertIn('"truncated": true', message)

    def test_provenance_records_actual_link_input_and_owner_output(self):
        graph = self.graph("bazel-out/k8-opt/bin/_solib_k8/owner", adapter="interface")
        link = graph["actions"][-2]
        result = closure.shared_owner_inputs(graph, link, link["arguments"], {"libteam.so.5.6.1", "libteamdctl.so.0.1.5"})
        self.assertEqual(result["libteam.so.5.6.1"], {
            "owner": "@@libteam+//:libteam_shared", "input": "bazel-out/k8-opt/bin/_solib_k8/owner/libteam.so.5.6.1",
            "producer_output": "bazel-out/k8-opt/bin/external/libteam+/libteam.so.5.6.1"})


if __name__ == "__main__":
    unittest.main()
