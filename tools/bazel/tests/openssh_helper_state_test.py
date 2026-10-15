"""Check retained OpenSSH state with Debian's real user-service purge helper."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).absolute().parents[3]
CONTRACT = ROOT / "platform/vs/docker-syncd-vs/config/runtime_package_state.json"
SOCKET_LINK = "etc/systemd/user/sockets.target.wants/ssh-agent.socket"
STATE_ROOT = "var/lib/systemd/deb-systemd-user-helper-enabled/"
STATE_PATHS = {
    STATE_ROOT + "sockets.target.wants/ssh-agent.socket",
    STATE_ROOT + "ssh-agent.socket.dsh-also",
}


@unittest.skipUnless(shutil.which("deb-systemd-helper"), "requires Debian's deb-systemd-helper")
class OpensshHelperStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract = json.loads(CONTRACT.read_text())

    def materialize(self, include_state):
        selected = {SOCKET_LINK} | (STATE_PATHS if include_state else set())
        entries = {e["path"]: e for e in self.contract["entries"] if e["path"] in selected}
        self.assertEqual(set(entries), selected)
        for name, entry in entries.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "symlink":
                path.symlink_to(entry["linkname"])
            else:
                data = entry["text"].encode()
                self.assertEqual(len(data), entry["size"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
                path.write_bytes(data)
                path.chmod(entry["mode"])

    def purge(self):
        # The package's unit has already been removed when postrm invokes this
        # helper. DPKG_ROOT confines every lookup and removal to this test root.
        result = subprocess.run(
            [shutil.which("deb-systemd-helper"), "--user", "purge", "ssh-agent.socket"],
            env=dict(os.environ, DPKG_ROOT=str(self.root), DPKG_MAINTSCRIPT_PACKAGE="openssh-client"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_purge_removes_socket_link_and_captured_helper_records(self):
        self.materialize(include_state=True)
        self.purge()
        for name in STATE_PATHS | {SOCKET_LINK}:
            self.assertFalse(os.path.lexists(self.root / name), name)

    def test_static_socket_link_without_helper_state_survives_purge(self):
        self.materialize(include_state=False)
        self.purge()
        self.assertTrue((self.root / SOCKET_LINK).is_symlink())

    def test_capture_matches_source_scripts_and_unit(self):
        reference = self.contract["reference"]["openssh_user_helper_state"]
        source_controls = ROOT / "tools/bazel/dpkg/source_controls"
        manifest = json.loads((source_controls / "manifest.json").read_text())
        self.assertEqual(reference["source_version"], manifest["packages"]["openssh-client"]["version"])
        for basename in ("postinst", "postrm"):
            original = (source_controls / "openssh-client" / basename).read_bytes()
            captured = reference["matching_files"]["var/lib/dpkg/info/openssh-client." + basename]
            self.assertEqual(hashlib.sha256(original).hexdigest(), captured["sha256"])
            self.assertEqual(len(original), captured["size"])
        unit = "usr/lib/systemd/user/ssh-agent.socket"
        self.assertEqual(reference["matching_files"][unit]["sha256"],
                         self.contract["source_package_files"][unit]["sha256"])


if __name__ == "__main__":
    unittest.main()
