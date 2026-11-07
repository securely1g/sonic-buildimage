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
    def check_graph(self, search, *, missing_library=False, parameter_file=False):
        """Exercise real link checks, stopping before any ELF or archive work."""
        class LinksAccepted(Exception):
            pass

        programs = ["usr/bin/teamsyncd", "usr/bin/tlm_teamd"]
        with tempfile.TemporaryDirectory(prefix="swss-owner-link-test-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "consumer.cpp").write_text("int main() { return 0; }\n")
            graph = {"targets": [], "actions": []}
            for number, program in enumerate(programs):
                name = Path(program).name
                graph["targets"].append({"id": number, "label": "@@sonic-swss+//:" + name})
                args = [search, "-l:libteamdctl.so.0.1.5"]
                if name == "teamsyncd" and not missing_library:
                    args.append("-l:libteam.so.5.6.1")
                action = {"targetId": number, "mnemonic": "CppLink", "arguments": args}
                if parameter_file:
                    action.update(arguments=[], paramFiles=[{"arguments": args}])
                graph["actions"].append(action)
            graph_path = root / "links.json"
            graph_path.write_text(json.dumps(graph))
            with mock.patch.object(closure.package_contract, "read_policy", return_value={"install_inventory": {}}), \
                 mock.patch.object(closure.package_contract, "validate_install_inventory", return_value=programs), \
                 mock.patch.object(closure.artifacts, "payload", return_value={}), \
                 mock.patch.object(closure.artifacts, "debug_archives", return_value={"debug_pairs": [{"path": p} for p in programs]}), \
                 mock.patch.object(closure.tarfile, "open", side_effect=LinksAccepted):
                try:
                    closure.verify(source, root / "runtime.tar", root / "symbols.tar", root / "policy.json", graph_path)
                except LinksAccepted:
                    return
                self.fail("link validation did not reach the archive boundary")

    def test_shared_owner_and_response_file_links_are_accepted(self):
        for response in (False, True):
            with self.subTest(response=response):
                self.check_graph("-Lbazel-out/k8-fastbuild/bin/external/libteam+/lib", parameter_file=response)

    def test_old_local_stock_and_similarly_named_providers_are_rejected(self):
        for search in ("-Lbazel-out/bin/external/+_repo_rules+libteam_Usource/lib",
                       "-L/usr/lib", "-Lbazel-out/bin/external/libteam-fake+/lib"):
            with self.subTest(search=search), self.assertRaisesRegex(ValueError, "shared libteam owner"):
                self.check_graph(search)

    def test_shared_directory_does_not_replace_required_library_check(self):
        with self.assertRaisesRegex(ValueError, "owner library outputs"):
            self.check_graph("-Lbazel-out/k8-fastbuild/bin/external/libteam+/lib", missing_library=True)


if __name__ == "__main__":
    unittest.main()
