import hashlib
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vs_host_base as host


class HostBaseTest(unittest.TestCase):
    def test_private_trial_can_only_be_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            trial = host.namespace(parent, "1234-1", True)
            self.assertEqual(host.namespace(parent, "1234-1", False), trial)
            with self.assertRaises(FileExistsError):
                host.namespace(parent, "1234-1", True)
            (trial / "cache").chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                host.namespace(parent, "1234-1", False)
            with self.assertRaisesRegex(ValueError, "invalid trial"):
                host.namespace(parent, "../global", False)

    def test_reject_corrupt_inventory_without_exposing_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "fixture"
            src.write_text("bytes")
            outputs = {"image": (src, host.IMAGE), "inventory": (src, host.INVENTORY)}
            expected = {"image": host.fingerprint(src), "inventory": {"sha256": "invalid", "bytes": 4}}
            with self.assertRaisesRegex(ValueError, "inventory"):
                host.restore_pair(repo, outputs, expected)
            self.assertFalse((repo / host.IMAGE).exists())
            self.assertFalse((repo / host.INVENTORY).exists())

    def test_cold_and_cache_only_warm_with_distinct_output_bases(self):
        name = str(random.SystemRandom().randrange(10**17, 10**18)) + "-1"
        control = host.control_dir(name)
        control.mkdir(mode=0o700)
        self.addCleanup(host.shutil.rmtree, control)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cache = host.namespace(repo, name, True) / "cache"
            identity = {"trial_id": name}
            (repo / host.IMAGE.parent).mkdir()
            script = repo / "build_debian.sh"
            script.write_text(
                '#!/bin/sh\nset -eu\nmkdir -p fs/etc/sonic target/versions/host-base-image\n'
                'echo stock > fs/etc/sonic/.vs-host-base-v1\n'
                f'/usr/bin/mksquashfs fs {host.IMAGE} -processors 1 -noappend\n'
                f'echo base-files==1 > {host.INVENTORY}\n')
            script.chmod(0o755)
            permit = {"identity_sha256": hashlib.sha256(host.data_bytes(identity)).hexdigest(),
                      "repo": str(repo), "env": {"PATH": "/usr/bin:/bin", "LANG": "C"}}
            (control / "permit.json").write_bytes(host.data_bytes(permit))
            bazel = host.bazel_binary()
            cold = repo / "cold"
            cold.mkdir()
            event, image, inventory = host.build(cold, cache, identity, bazel)
            self.assertFalse(event.get("cacheHit", False))
            self.assertEqual(event.get("runner"), "local")
            expected = [host.fingerprint(f) for f in (image, inventory)]
            (repo / host.IMAGE).unlink()
            (repo / host.INVENTORY).unlink()
            script.unlink()
            (control / "executed.json").unlink()
            warm = repo / "warm"
            warm.mkdir()
            event, image, inventory = host.build(warm, cache, identity, bazel)
            self.assertTrue(event.get("cacheHit"))
            self.assertEqual(event.get("runner"), "disk cache hit")
            self.assertEqual(expected, [host.fingerprint(f) for f in (image, inventory)])
            self.assertFalse((control / "executed.json").exists())
            empty = repo / "miss"
            empty.mkdir()
            with self.assertRaises(host.subprocess.CalledProcessError):
                host.build(empty, cache, {**identity, "kernel": "different bytes"}, bazel)
            self.assertFalse((repo / host.IMAGE).exists())


if __name__ == "__main__":
    unittest.main()
