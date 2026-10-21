"""Reject layered/deleted-file inputs instead of copying an incomplete rootfs."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.oci.single_layer_rootfs import copy_rootfs


class SingleLayerRootfsTest(unittest.TestCase):
    def test_copies_existing_blob_without_repacking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blob, output = root / "blob", root / "output.tar.gz"
            blob.write_bytes(b"original compressed filesystem bytes")
            layout = SimpleNamespace(layers=[blob], manifest={"layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]})
            with patch("tools.bazel.oci.single_layer_rootfs.validate_layout", return_value=layout) as validate:
                copy_rootfs(root, "arm64", output)
            validate.assert_called_once_with(root, "linux/arm64")
            self.assertEqual(output.read_bytes(), blob.read_bytes())

    def test_rejects_multiple_layers_even_when_last_one_has_a_filesystem(self):
        layout = SimpleNamespace(layers=[Path("base"), Path("whiteout-layer")])
        with patch("tools.bazel.oci.single_layer_rootfs.validate_layout", return_value=layout):
            with self.assertRaisesRegex(ValueError, "materialize multi-layer"):
                copy_rootfs(Path("image"), "amd64", Path("output"))

    def test_rejects_unexpected_layer_format(self):
        layout = SimpleNamespace(layers=[Path("blob")], manifest={"layers": [{"mediaType": "application/octet-stream"}]})
        with patch("tools.bazel.oci.single_layer_rootfs.validate_layout", return_value=layout):
            with self.assertRaisesRegex(ValueError, "gzip TAR"):
                copy_rootfs(Path("image"), "amd64", Path("output"))


if __name__ == "__main__":
    unittest.main()
