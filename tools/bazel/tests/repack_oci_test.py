"""Real upstream umoci replacement preserves config and deletes obsolete files."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.oci import repack_oci
from tools.bazel.oci.oci_inventory import apply_layer
from tools.bazel.oci.oci_layout import validate_layout
from tools.bazel.tests.oci_base_fixture import image_fixture, oci_files, write_layout

UMOCI = TAR = None


def tool_path(value):
    path = Path(value)
    if not path.exists() and value.startswith('external/'):
        path = Path('..') / value.removeprefix('external/')
    return str(path.absolute())


class UpstreamRepackTest(unittest.TestCase):
    def test_replaces_files_and_keeps_all_image_configuration(self):
        if not UMOCI or not TAR:
            self.skipTest('Run the Bazel target with native umoci and BSD tar')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / 'base'
            config, layers = image_fixture()
            original = json.loads(config)
            original['config'].update(User='123:456', StopSignal='SIGTERM', Volumes={'/data': {}})
            write_layout(base, oci_files(json.dumps(original).encode(), layers))
            before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()}
            rootfs = root / 'rootfs.tar'
            with tarfile.open(rootfs, 'w') as stream:
                item = tarfile.TarInfo('data')
                item.type, item.mode = tarfile.DIRTYPE, 0o755
                stream.addfile(item)
                item = tarfile.TarInfo('data/installed')
                item.size, item.uid, item.gid, item.mode = 7, 123, 456, 0o2755
                stream.addfile(item, io.BytesIO(b'new ELF'))
            outputs = []
            for number in range(2):
                output = root / str(number)
                with patch.object(sys, 'argv', ['repack', '--base', str(base), '--rootfs', str(rootfs),
                                  '--platform', 'linux/amd64', '--umoci', tool_path(UMOCI),
                                  '--tar', tool_path(TAR), '--output', str(output)]):
                    repack_oci.main()
                checked = validate_layout(output, 'linux/amd64')
                self.assertEqual(checked.config['config'], original['config'])
                self.assertEqual(checked.config['rootfs']['diff_ids'][:2], original['rootfs']['diff_ids'])
                files = {}
                for layer in checked.layers:
                    apply_layer(layer, files)
                self.assertNotIn('etc/base', files)
                self.assertNotIn('etc/second', files)
                self.assertNotIn('etc/deleted', files)
                self.assertEqual((files['data/installed']['uid'], files['data/installed']['gid'],
                                  files['data/installed']['mode']), (123, 456, 0o2755))
                outputs.append(checked.descriptor['digest'])
            self.assertEqual(outputs[0], outputs[1], 'repacking the same input must be reproducible')
            self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob('*') if p.is_file()})


if __name__ == '__main__':
    UMOCI, TAR = sys.argv[1:]
    unittest.main(argv=[sys.argv[0]])
