"""Compare native SONiC stage-one compression with its real cached Bazel action."""

import errno
import importlib.util
import os
import platform
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "tools/bazel/image/intermediate_squashfs.py"
STAMP = 1700000000
BAZEL = shutil.which(os.environ.get("BAZEL", "bazel"))
REQUIRED = ("tar", "mksquashfs", "unsquashfs", "findmnt", "ldd")
HIT = "SonicSquashfs: disk cache hit"
MISS = "SonicSquashfs: executed locally"


def command(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    if result.returncode:
        raise AssertionError(f"{args[0]} exited {result.returncode}:\n{result.stdout}")
    return result.stdout


def add_xattr(path, value):
    try:
        os.setxattr(path, "user.sonic-test", value)
    except OSError as exc:
        if exc.errno != errno.ENOTSUP:
            raise


def fixture(root):
    root.mkdir(mode=0o751)
    subdir = root / "usr" / "lib" / "sonic"
    subdir.mkdir(parents=True)
    program = subdir / "probe"
    program.write_bytes(b"SONiC\0payload\xff" * 16000)
    os.chmod(program, 0o4751)
    os.link(program, subdir / "probe.hardlink")
    (subdir / "probe.symlink").symlink_to("probe")
    os.mkfifo(subdir / "notify", 0o620)
    add_xattr(root, b"root\x00\xff\nmeta")
    add_xattr(subdir, b"dir\x00\xfe\nmeta")
    add_xattr(program, b"file\x00\xfd\nmeta")
    for i, path in enumerate([root, *sorted(root.rglob("*"))]):
        os.utime(path, (STAMP + i, STAMP + i), follow_symlinks=False)


def image_metadata(image):
    # Block size, compressor options, fragment packing and NFS exports are also
    # part of compatibility. The padded image size and creation date can differ.
    info = command(["unsquashfs", "-stat", image])
    options = [line.strip() for line in info.splitlines()
               if not line.startswith(("Found a valid ", "Creation or last append time ", "Filesystem size "))]
    return options, command(["unsquashfs", "-lln", "-UTC", "-full-precision", image])


def extracted_tree(root):
    # Directory order and allocated inode numbers can differ between formats.
    # Record groups of hardlinked files by their names inside each restored tree.
    files, links = {}, {}
    for path in [root, *sorted(root.rglob("*"))]:
        rel = path.relative_to(root).as_posix()
        st = path.lstat()
        if stat.S_ISREG(st.st_mode):
            value = path.read_bytes()
            links.setdefault((st.st_dev, st.st_ino), []).append(rel)
        elif stat.S_ISLNK(st.st_mode):
            value = os.readlink(path)
        elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
            value = (os.major(st.st_rdev), os.minor(st.st_rdev))
        else:
            value = None
        xattrs = {name: os.getxattr(path, name, follow_symlinks=False)
                  for name in os.listxattr(path, follow_symlinks=False)}
        files[rel] = (st.st_mode, st.st_uid, st.st_gid, st.st_mtime_ns, xattrs, value)
    return files, sorted(group for group in links.values() if len(group) > 1)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "x86_64",
                     "The private VS compression action requires Linux AMD64")
@unittest.skipUnless(BAZEL and all(shutil.which(name) for name in REQUIRED),
                     "Bazel, GNU tar, squashfs-tools, findmnt and ldd are required")
class IntermediateSquashfsCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Plain Bazelisk can exist without the offline binary used by the VS
        # slave. Avoid starting a separate download on an unprovisioned runner.
        spec = importlib.util.spec_from_file_location("vs_compression", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        try:
            cls.bazel = helper.bazel_binary(Path(BAZEL))
        except ValueError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        if "-tar" not in command(["mksquashfs", "-help"]):
            raise unittest.SkipTest("Install squashfs-tools with tar input support")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.cache = self.work / "private-cache"
        self.cache.mkdir(mode=0o700)
        self.env = {**os.environ, "LC_ALL": "C"}
        # The unprivileged fixture owns every captured inode. Exercise the real
        # GNU tar invocation without asking a hosted runner for a root password.
        if os.getuid() != 0:
            bin_dir = self.work / "bin"
            bin_dir.mkdir()
            sudo = bin_dir / "sudo"
            sudo.write_text('#!/bin/sh\n'
                            '[ "$1" = -n ] && [ "$2" = -- ] && [ "$3" = /usr/bin/tar ] || exit 65\n'
                            'shift 2\nexec "$@"\n')
            sudo.chmod(0o700)
            self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]

    def compress(self, root, name, expected):
        # Each run has a fresh destination and the helper creates a fresh Bazel
        # output base. Only the explicitly shared private disk cache can hit.
        out = self.work / name
        out.mkdir()
        image = out / "sonic-vs.bin__vs__rfs.squashfs"
        log = command([sys.executable, HELPER, "--root", root, "--output", image,
                       "--bazel", self.bazel, "--cache-dir", self.cache], cwd=REPO, env=self.env)
        receipts = [line for line in log.splitlines() if line.startswith("SonicSquashfs:")]
        self.assertIn(expected, receipts, log)
        self.assertNotIn(HIT if expected == MISS else MISS, receipts, log)
        return image

    def compare_with_native(self, root, image):
        reference = image.with_name("native.squashfs")
        # Match the slave's sudo env_reset when invoking the native reference.
        native_env = dict(self.env)
        native_env.pop("SOURCE_DATE_EPOCH", None)
        command(["mksquashfs", root, reference, "-Xcompression-level", "1", "-processors", "2", "-no-progress"],
                env=native_env)
        expected_options, expected_inodes = image_metadata(reference)
        actual_options, actual_inodes = image_metadata(image)
        for required in ("Compression gzip", "compression-level 1", "Block size 131072",
                         "Filesystem is exportable via NFS", "Always-use-fragments option is not specified"):
            self.assertIn(required, expected_options)
        self.assertEqual(actual_options, expected_options)
        self.assertEqual(actual_inodes, expected_inodes)
        trees = []
        for src in (reference, image):
            dest = src.with_suffix(".extracted")
            command(["unsquashfs", "-strict-errors", "-no-progress", "-processors", "2", "-dest", dest, src])
            trees.append(extracted_tree(dest))
        self.assertEqual(trees[1], trees[0])
        self.assertEqual(trees[0][1], [["usr/lib/sonic/probe", "usr/lib/sonic/probe.hardlink"]])
        self.assertEqual(trees[0][0]["."][3], root.stat().st_mtime_ns // 10**9 * 10**9)
        self.assertEqual(trees[0][0]["."][4], {k: os.getxattr(root, k) for k in os.listxattr(root)})

    def test_fresh_output_hit_and_content_and_metadata_misses(self):
        first, second = self.work / "worker-a", self.work / "worker-b"
        fixture(first)
        fixture(second)
        cold = self.compress(first, "cold", MISS)
        self.compare_with_native(first, cold)
        warm = self.compress(second, "warm", HIT)
        self.assertEqual(warm.read_bytes(), cold.read_bytes())

        # Change bytes without allowing the file timestamp or mode to change.
        program = second / "usr/lib/sonic/probe"
        st = program.stat()
        with program.open("r+b") as out:
            out.write(b"sonic")
        os.chmod(program, stat.S_IMODE(st.st_mode))
        os.utime(program, ns=(st.st_atime_ns, st.st_mtime_ns))
        changed = self.compress(second, "content-change", MISS)
        self.compare_with_native(second, changed)

        # Change only root inode metadata and keep all file payloads identical.
        os.chmod(second, 0o711)
        add_xattr(second, b"changed\x00\xff")
        metadata = self.compress(second, "metadata-change", MISS)
        self.compare_with_native(second, metadata)

    def test_privileged_numeric_ownership_device_and_file_capability(self):
        if os.getuid() != 0 or not shutil.which("setcap"):
            self.skipTest("Requires chown, mknod and setcap privileges")
        root = self.work / "privileged"
        fixture(root)
        dev = root / "dev"
        dev.mkdir()
        probe = root / "usr/lib/sonic/probe"
        try:
            os.mknod(dev / "null", stat.S_IFCHR | 0o660, os.makedev(1, 3))
            os.chown(root, 233, 234)
            os.chown(probe, 235, 236)
        except PermissionError:
            self.skipTest("The test host lacks chown or mknod capability")
        cap = subprocess.run(["setcap", "cap_net_raw=ep", probe], capture_output=True)
        if cap.returncode:
            self.skipTest("The test host cannot set file capabilities")
        self.assertIn("security.capability", os.listxattr(probe))
        image = self.compress(root, "privileged-output", MISS)
        self.compare_with_native(root, image)


if __name__ == "__main__":
    unittest.main()
