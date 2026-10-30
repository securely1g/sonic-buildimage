"""Exercise upstream umoci on the multi-layer Make compatibility input."""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import unpack_oci
from tools.bazel.tests.oci_base_fixture import digest, image_fixture, oci_files, write_layout


def tool_path(value):
    path = Path(value)
    if not path.exists() and value.startswith('external/'):
        path = Path('..') / value.removeprefix('external/')
    return str(path.absolute())


UMOCI = TAR = None


class UpstreamUnpackTest(unittest.TestCase):
    def test_whiteouts_numeric_owners_and_links(self):
        if not UMOCI or not TAR:
            self.skipTest("Run the Bazel target for the declared native umoci and BSD tar tools")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = root / 'image'
            config, layers = image_fixture()
            layer = io.BytesIO()
            with tarfile.open(fileobj=layer, mode='w') as stream:
                item = tarfile.TarInfo('etc/owned')
                item.size, item.uid, item.gid, item.mode = 5, 123, 456, 0o2755
                stream.addfile(item, io.BytesIO(b'owned'))
                link = tarfile.TarInfo('etc/link')
                link.type, link.linkname = tarfile.SYMTYPE, 'owned'
                stream.addfile(link)
            layers.append(layer.getvalue())
            parsed = json.loads(config)
            parsed['rootfs']['diff_ids'] = [digest(value) for value in layers]
            parsed['history'].append({'created_by': 'ownership fixture'})
            write_layout(layout, oci_files(json.dumps(parsed).encode(), layers))
            before = {str(p.relative_to(layout)): p.read_bytes() for p in layout.rglob('*') if p.is_file()}
            output = root / 'rootfs.tar'
            with patch.object(sys, 'argv', ['unpack', '--image', str(layout), '--platform', 'linux/amd64',
                              '--umoci', tool_path(UMOCI), '--tar', tool_path(TAR), '--output', str(output)]):
                unpack_oci.main()
            with tarfile.open(output) as stream:
                entries = {item.name.removeprefix('./'): item for item in stream}
                self.assertNotIn('etc/deleted', entries)
                self.assertFalse(any('.wh.' in name for name in entries))
                self.assertEqual(stream.extractfile(entries['etc/base']).read(), b'updated by second layer\n')
                owned = entries['etc/owned']
                self.assertEqual((owned.uid, owned.gid, owned.mode), (123, 456, 0o2755))
                self.assertTrue(entries['etc/link'].issym())
                self.assertEqual(entries['etc/link'].linkname, 'owned')
            self.assertEqual(before, {str(p.relative_to(layout)): p.read_bytes() for p in layout.rglob('*') if p.is_file()})


if __name__ == '__main__':
    UMOCI, TAR = sys.argv[1:]
    unittest.main(argv=[sys.argv[0]])
