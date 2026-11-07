"""Small synthetic archives verify layer containment without running any tools."""

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from extractor import _unpack_docker_archive


class LayerContainmentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim"
        self.victim.write_bytes(b"unchanged")
        self.sequence = 0
        for target in ("subprocess.run", "subprocess.Popen", "socket.socket"):
            blocker = patch(target, side_effect=AssertionError("No process/network allowed"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def archive(self, layers, *, manifest_layers=None):
        self.sequence += 1
        path = self.base / f"image-{self.sequence}.gz"
        names = [f"layer-{n}.tar" for n in range(len(layers))]
        with tarfile.open(path, "w:gz") as outer:
            files = {"manifest.json": json.dumps([{
                "Layers": names if manifest_layers is None else manifest_layers
            }]).encode()}
            for name, members in zip(names, layers):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as layer:
                    for member_name, kind, value in members:
                        entry = tarfile.TarInfo(member_name)
                        if kind == "file":
                            entry.size = len(value)
                            layer.addfile(entry, io.BytesIO(value))
                        else:
                            entry.type = {"symlink": tarfile.SYMTYPE,
                                          "hardlink": tarfile.LNKTYPE}[kind]
                            entry.linkname = value
                            layer.addfile(entry)
                files[name] = stream.getvalue()
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                outer.addfile(entry, io.BytesIO(data))
        return path, self.base / f"unpacked-{self.sequence}" / "root"

    def rejected(self, layers):
        archive, root = self.archive(layers)
        with self.assertRaises(tarfile.FilterError):
            _unpack_docker_archive(archive, root)
        self.assertEqual(self.victim.read_bytes(), b"unchanged")

    def test_member_and_hardlink_traversal_are_rejected(self):
        for name in ("../outside/victim", str(self.victim)):
            with self.subTest(kind="file", name=name):
                self.rejected([[(name, "file", b"overwrite")]])
            with self.subTest(kind="hardlink", name=name):
                self.rejected([[('inside', 'hardlink', name)]])

    def test_manifest_layer_references_stay_inside_outer_archive(self):
        for name in ('../outside/victim', str(self.victim)):
            with self.subTest(name=name):
                archive, root = self.archive([], manifest_layers=[name])
                with self.assertRaises(tarfile.FilterError):
                    _unpack_docker_archive(archive, root)
                self.assertEqual(self.victim.read_bytes(), b'unchanged')

    def test_whiteout_traversal_and_invalid_targets_are_rejected(self):
        for name in ("../outside/.wh.victim", str(self.outside / '.wh.victim'),
                     '.wh.', '.wh..', '.wh...'):
            with self.subTest(name=name):
                self.rejected([[(name, "file", b"")]])

    def test_lower_layer_symlink_cannot_redirect_write_link_or_whiteout(self):
        lower = [("escape", "symlink", str(self.outside))]
        for upper in ([('escape/victim', 'file', b'overwrite')],
                      [('inside', 'hardlink', 'escape/victim')],
                      [('escape/.wh.victim', 'file', b'')],
                      [('escape/.wh..wh..opq', 'file', b'')]):
            with self.subTest(upper=upper):
                self.rejected([lower, upper])

    def test_valid_layers_keep_literal_links_and_apply_whiteouts(self):
        archive, root = self.archive([
            [('usr/bin/tool', 'file', b'program'),
             ('usr/bin/relative', 'symlink', 'tool'),
             ('var/run', 'symlink', '/run'),
             ('old', 'file', b'remove'),
             ('remove-link', 'symlink', str(self.victim)),
             ('cache/old', 'file', b'remove')],
            [('usr/bin/hard', 'hardlink', 'usr/bin/tool'),
             ('.wh.old', 'file', b''),
             ('.wh.remove-link', 'file', b''),
             ('cache/.wh..wh..opq', 'file', b''),
             ('cache/new', 'file', b'keep')],
        ])
        _unpack_docker_archive(archive, root)
        self.assertEqual((root / 'usr/bin/tool').read_bytes(), b'program')
        self.assertTrue((root / 'usr/bin/hard').samefile(root / 'usr/bin/tool'))
        self.assertEqual((root / 'usr/bin/relative').readlink(), Path('tool'))
        self.assertEqual((root / 'var/run').readlink(), Path('/run'))
        self.assertFalse((root / 'old').exists())
        self.assertFalse((root / 'remove-link').is_symlink())
        self.assertFalse((root / 'cache/old').exists())
        self.assertEqual((root / 'cache/new').read_bytes(), b'keep')
        self.assertEqual(self.victim.read_bytes(), b'unchanged')


if __name__ == '__main__':
    unittest.main()
