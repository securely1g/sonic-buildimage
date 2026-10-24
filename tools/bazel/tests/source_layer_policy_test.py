"""Check that the image installs the same source modes its contract validates.

Tiny TARs reproduce Common's executable Lua inputs without compiling project
code. The actual Syncd BUILD wiring chooses the input to the production source
assembler and normalizer. Real bsdtar exclusion/notice behavior is exercised by
the separate oci_license_filter_test; these fixture paths survive that filter.
"""

import ast
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.bazel.ci.artifact_validation import payload
from tools.bazel.oci import normalize_layer, package_contract
from tools.bazel.oci.oci_inventory import apply_layer


def declaration(tree, rule, name):
    """Read one real BUILD declaration without executing unrelated targets."""
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if not isinstance(call.func, ast.Name) or call.func.id != rule:
            continue
        attributes = {item.arg: item.value for item in call.keywords}
        if ast.literal_eval(attributes.get("name", ast.Constant(None))) == name:
            return attributes
    raise AssertionError(f"Missing {rule} target {name}")


def write_tar(path, entries):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, mode, gid, link in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.gid = mode, gid
            if link is not None:
                member.type, member.linkname = tarfile.SYMTYPE, link
            elif data is None:
                member.type = tarfile.DIRTYPE
            else:
                member.size = len(data)
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)


class SourceLayerPolicyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-layer-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        owner = ROOT / "platform/vs/docker-syncd-vs"
        tree = ast.parse((owner / "BUILD.bazel").read_text())
        self.layer = declaration(tree, "normalize_layer", "source_runtime_layer")
        self.filtered = declaration(tree, "dpkg_filter", "rdeps")
        policies = json.loads((owner / "config/source_paths.json").read_text())["packages"]
        modes = policies["libswsscommon"]["path_modes"]
        self.lua = {name: mode for name, mode in modes.items() if name.endswith(".lua")}
        self.assertTrue(self.lua, "The image contract must exercise Common Lua inputs")
        self.contents = {name: ("source bytes: " + name + "\n").encode() for name in modes}
        common = [(name.replace("run/", "var/run/", 1) if name.startswith("run/") else name,
                   self.contents[name], 0o755, 0, None) for name in modes]
        self.ssh_bytes = b"source-built ssh-agent fixture\n"
        ssh = [("usr/bin/ssh-agent", self.ssh_bytes, 0o755, 0, None)]
        packages = {}
        for name, entries in (("libswsscommon", common), ("openssh-client", ssh)):
            packages[name] = self.root / (name + ".tar")
            write_tar(packages[name], entries)
        self.raw = self.root / "raw.tar"
        write_tar(self.raw, common + ssh)
        self.checked = self.root / "checked.tar"
        contract = {"architecture": "amd64", "packages": [
            {"package": "libswsscommon", "required_paths": {}, "path_modes": modes},
            {"package": "openssh-client", "required_paths": {},
             "path_modes": {"usr/bin/ssh-agent": policies["openssh-client"]["path_modes"]["usr/bin/ssh-agent"]},
             "path_owners": policies["openssh-client"]["path_owners"]},
        ]}
        package_contract.combine_tars(contract, packages, self.checked, {})
        # Use a different installed group from the static contract to prove the
        # real configured baseline still controls ssh-agent's metadata.
        self.base = self.root / "configured.tar"
        aliases = normalize_layer.DIRECTORY_ALIASES
        entries = [(target, None, 0o755, 0, None) for target in sorted({value["target"] for value in aliases.values()})]
        entries += [(name, None, 0o777, 0, value["linkname"]) for name, value in aliases.items()]
        entries += [("usr/bin/ssh-agent", b"Debian baseline bytes", 0o2755, 113, None)]
        write_tar(self.base, entries)

    def assembled_layer(self):
        """Follow the actual source edge; reverting to raw TARs changes the result."""
        source = ast.literal_eval(self.layer["src"])
        self.assertEqual(ast.literal_eval(self.filtered["src"]), ":source_runtime_payloads")
        # None of these fixture paths are filtered. The public rdeps edge must
        # remain the dpkg_filter target, retaining the real exclusion pass.
        inputs = {":rdeps": self.checked, ":source_runtime_files": self.raw}
        self.assertIn(source, inputs, "Review the changed image source pipeline")
        result = self.root / "installed.tar"
        options = {key: ast.literal_eval(self.layer[key]) for key in
                   ("expected_platform", "root_owned", "preserve_metadata")}
        options["modes"] = ast.literal_eval(self.layer["modes"]) if "modes" in self.layer else {}
        normalize_layer.normalize(inputs[source], self.base, result, **options)
        return result

    def test_image_lua_modes_match_the_checked_source_contract(self):
        """Prevent executable owner Lua inputs from bypassing the image's 0644 policy."""
        installed = payload(self.assembled_layer(), require_root=False)
        for name, mode in self.lua.items():
            with self.subTest(path=name):
                self.assertEqual(installed[name], {
                    "kind": "file", "mode": mode, "uid": 0, "gid": 0,
                    "sha256": hashlib.sha256(self.contents[name]).hexdigest(),
                    "size": len(self.contents[name]),
                })
        self.assertEqual(installed["run/redis/sonic-db/database_config.json"]["mode"], 0o644)

    def test_checked_payload_keeps_configured_ssh_agent_metadata_and_run_alias(self):
        """Applying source policy must preserve Debian's setgid assignment and /var/run link."""
        installed = {}
        apply_layer(self.base, installed)
        apply_layer(self.assembled_layer(), installed)
        self.assertEqual(installed["usr/bin/ssh-agent"], {
            "kind": "file", "mode": 0o2755, "uid": 0, "gid": 113,
            "sha256": hashlib.sha256(self.ssh_bytes).hexdigest(), "size": len(self.ssh_bytes),
        })
        self.assertEqual(installed["var/run"], {
            "kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0, "linkname": "/run",
        })


if __name__ == "__main__":
    unittest.main()
