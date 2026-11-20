#!/usr/bin/env python3
"""Cache compression of a completed Make rootfs in a private, worker-local Bazel cache."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


HERE = Path(__file__).resolve().parent
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}
LOCAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs"}
# The VS installer builds on Linux AMD64 with the repository's Bazel 8.5.1.
# Resolve the cached distribution before capturing the filesystem. A Bazelisk
# or CI wrapper could otherwise inject flags, invoke another build or download.
BAZEL_SHA256 = "61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e"


def require_local(directory):
    filesystem = subprocess.check_output(
        ["/usr/bin/findmnt", "-n", "-o", "FSTYPE", "--target", directory], env=ENV, text=True,
    ).strip()
    if filesystem not in LOCAL_FILESYSTEMS:
        raise ValueError("private cache requires a local filesystem")


def prepare_cache(path):
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError("private cache requires an absolute directory")
    trusted = {os.geteuid(), os.stat("/").st_uid, 0}
    for directory in [*reversed(path.parents), path]:
        try:
            st = directory.lstat()
        except FileNotFoundError:
            # Check the enclosing mount before creating anything, including on
            # hosts where the candidate path points at the shared Make NFS cache.
            require_local(directory.parent)
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid not in trusted:
            raise ValueError("untrusted private cache directory or ancestor")
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise ValueError("private cache ancestor is writable by others")
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError("private cache must be owned by the build user and have mode 0700")
    require_local(path)
    # The mounted directory is operator-selected. Only claim an empty
    # directory. All data subject to GC lives below our versioned namespace.
    storage = path / "sonic-vs-compression-v1"
    marker = storage / "format"
    signature = b"SONiC private VS SquashFS compression cache v1\n"
    # flock the existing directory itself: checking ownership or initializing
    # the namespace must not create a lockfile in an unrelated directory.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not storage.exists():
            if any(path.iterdir()):
                raise ValueError("unrecognized nonempty private cache directory")
            storage.mkdir(mode=0o700)
            with marker.open("xb") as stream:
                stream.write(signature)
        st = storage.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise ValueError("untrusted private cache namespace")
        if (marker.is_symlink() or not marker.is_file() or marker.stat().st_size != len(signature)
                or marker.read_bytes() != signature):
            raise ValueError("unrecognized private cache format")
    finally:
        os.close(fd)
    return storage


def bazel_binary(candidate):
    bazelisk = Path(os.environ.get("BAZELISK_HOME", Path.home() / ".cache/bazelisk"))
    for path in [candidate, bazelisk / "downloads/sha256" / BAZEL_SHA256 / "bin/bazel"]:
        if path is not None and path.is_file():
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == BAZEL_SHA256:
                    return path.resolve()
    raise ValueError("pinned Linux AMD64 Bazel 8.5.1 executable is unavailable")


def capture(root, archive):
    if not root.is_dir() or root.is_symlink():
        raise ValueError("expected a prepared filesystem directory")
    # Run only GNU tar with privileges so protected data and security xattrs
    # are readable. The caller creates the private output and owns Bazel.
    command = ["/usr/bin/tar", "--create", "--file=-", "--format=pax", "--sort=name",
               "--numeric-owner", "--xattrs", "--xattrs-include=*", "--selinux", "--acls",
               "--pax-option=delete=atime,delete=ctime,exthdr.name=%d/PaxHeaders/%f",
               "--directory", root, "."]
    if os.geteuid() == 0 and "SOURCE_DATE_EPOCH" in os.environ:
        raise ValueError("native SquashFS timestamp clamping is configured")
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise ValueError("privileged rootfs capture requires sudo")
        if "SOURCE_DATE_EPOCH" in os.environ:
            # Make exports an epoch to the slave, but the ordinary sudo policy
            # drops it before mksquashfs. Custom policies may retain it and
            # thereby change inode timestamps; defer those to the native path.
            check = subprocess.run([sudo, "-n", "--", "/usr/bin/printenv", "SOURCE_DATE_EPOCH"],
                                   env=dict(ENV, SOURCE_DATE_EPOCH=os.environ["SOURCE_DATE_EPOCH"]),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if check.returncode != 1 or check.stderr:
                raise ValueError("native SquashFS timestamp clamping could not be excluded")
        command = [sudo, "-n", "--", *command]
    with archive.open("xb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, env=ENV)
    # GNU tar can return success when it skips a socket or an unreadable xattr.
    # Make's directory compressor handles these trees using its normal path.
    if result.returncode or result.stderr:
        raise ValueError("rootfs snapshot was incomplete or changed during capture")


def root_metadata(archive, attrs_file):
    with tarfile.open(archive, "r:") as stream:
        root = stream.next()
    if root is None or not root.isdir() or root.name not in {".", "./"}:
        raise ValueError("missing root inode in snapshot")
    # mksquashfs' tar reader drops the root entry. Explicitly restore its
    # metadata. Keep even binary xattrs out of Bazel's argv and execution log.
    with attrs_file.open("x", encoding="ascii") as attrs:
        for key, value in sorted(root.pax_headers.items()):
            if key.startswith("SCHILY.xattr."):
                name = key.removeprefix("SCHILY.xattr.")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                    raise ValueError("unrepresentable root xattr name")
                attrs.write("/ x " + name + "=0x" + os.fsencode(value).hex() + "\n")
    mtime = str(int(root.mtime))
    return ["-root-mode", format(root.mode & 0o7777, "o"), "-root-uid", str(root.uid),
            "-root-gid", str(root.gid), "-root-time", mtime, "-mkfs-time", mtime]


def tool_runtime(workspace):
    runtime = workspace / "runtime"
    libs = runtime / "lib"
    libs.mkdir(parents=True)
    # The Trixie slave pins squashfs-tools 4.6.1. Its TAR-specific defaults
    # and root metadata handling are checked by the directory-equivalence test.
    mksquashfs = Path("/usr/bin/mksquashfs")
    version = subprocess.check_output([mksquashfs, "-version"], env=ENV, text=True).splitlines()
    if not version or not version[0].startswith("mksquashfs version 4.6.1 "):
        raise ValueError("unsupported native squashfs-tools release")
    preload = Path("/etc/ld.so.preload")
    if preload.exists() and any(line.split("#", 1)[0].strip() for line in preload.read_text().splitlines()):
        raise ValueError("system-wide preloaded libraries are configured")
    loaders = set()
    for name, tool in [("sh", Path("/bin/sh")), ("mksquashfs", mksquashfs)]:
        # Resolve trusted tools installed in the slave; every byte read by the
        # compressor is declared under a stable workspace-relative filename.
        shutil.copy2(tool, runtime / name)
        for line in subprocess.check_output(["/usr/bin/ldd", tool], env=ENV, text=True).splitlines():
            entry = line.strip().split()
            if not entry or entry[0].startswith("linux-vdso"):
                continue
            if len(entry) == 2 and entry[0].startswith("/"):
                loaders.add(Path(entry[0]).resolve())
            elif len(entry) == 4 and entry[1] == "=>" and entry[2].startswith("/"):
                library = Path(entry[2])
                destination = libs / entry[0]
                if destination.exists():
                    if destination.read_bytes() != library.read_bytes():
                        raise ValueError("conflicting native compression libraries")
                else:
                    shutil.copy2(library, destination)
            else:
                raise ValueError("unsupported native compression tool dependency")
    if len(loaders) != 1:
        raise ValueError("unsupported native compression tool loader")
    shutil.copy2(loaders.pop(), runtime / "ld.so")


def trim_cache(disk):
    if not disk.exists():
        return
    # Batch Bazel exits before its idle-time GC runs. Rotate only our private
    # action data, under the same lock used by builds. Large snapshots never
    # remain in Bazel's output base or in the workspace after the invocation.
    size = int(subprocess.check_output(["/usr/bin/du", "-sb", disk], env=ENV).split()[0])
    if size >= 16 * 1024**3:
        shutil.rmtree(disk)


def compress(root, output, cache, bazel):
    cache = prepare_cache(cache)
    executable = bazel_binary(bazel)
    if root.is_symlink():
        raise ValueError("expected a prepared filesystem directory")
    root = root.resolve(strict=True)
    if any(path.is_relative_to(root) for path in [cache.resolve(), output.parent.resolve()]):
        raise ValueError("cache and output must be outside the source tree")
    with (cache / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        disk = cache / "action-cache"
        trim_cache(disk)
        # A fixed relative archive name allows independent captures to share a
        # Bazel action key. A fresh output base makes every hit use the disk cache.
        captures = cache / "captures"
        captures.mkdir(mode=0o700, exist_ok=True)
        # A cancelled job may leave a private TAR. The Bazel subprocess also
        # holds the lock, so the next build can remove abandoned job data here.
        for leftover in captures.glob("job-*"):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
        with tempfile.TemporaryDirectory(dir=captures, prefix="job-") as temp:
            directory = Path(temp)
            workspace = directory / "workspace"
            workspace.mkdir()
            archive = workspace / "root.tar"
            start = time.monotonic()
            capture(root, archive)
            options = root_metadata(archive, workspace / "root.pseudo")
            print(f"SquashFS snapshot: {archive.stat().st_size} bytes in {time.monotonic() - start:.3f}s",
                  flush=True)
            tool_runtime(workspace)
            shutil.copy2(HERE / "private_squashfs.bzl", workspace / "compress.bzl")
            (workspace / "WORKSPACE").write_text('workspace(name = "private_sonic_squashfs")\n')
            # No external repositories or toolchains are required for this action.
            (workspace / "resolved.bzl").write_text("resolved = []\n")
            (workspace / "BUILD").write_text(
                'load(":compress.bzl", "private_squashfs")\n'
                'platform(name="native")\n'
                'private_squashfs(name="image", archive="root.tar", attributes="root.pseudo",'
                ' loader="runtime/ld.so", inputs=glob(["runtime/**"]), root_options='
                + json.dumps(options) + ')\n'
            )
            actions = directory / "actions.json"
            env = dict(ENV, HOME=temp, TMPDIR=temp)
            command = [executable, "--batch", "--ignore_all_rc_files",
                       f"--output_user_root={cache / 'bazel-user'}", f"--output_base={directory / 'base'}",
                       "--host_jvm_args=-XX:ActiveProcessorCount=2", "build", "--jobs=1",
                       "--noenable_bzlmod", "--enable_workspace", "--incompatible_autoload_externally=",
                       f"--experimental_resolved_file_instead_of_workspace={workspace / 'resolved.bzl'}",
                       "--repository_disable_download", "--platforms=//:native", "--host_platform=//:native",
                       "--remote_cache=", "--remote_executor=", "--bes_backend=", f"--disk_cache={disk}",
                       f"--execution_log_json_file={actions}", "//:image"]
            bazel_start = time.monotonic()
            result = subprocess.run(command, cwd=workspace, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, pass_fds=(lock.fileno(),))
            bazel_seconds = time.monotonic() - bazel_start
            if result.returncode:
                raise ValueError("private Bazel compression failed")
            event = json.loads(actions.read_text())
            if event.get("mnemonic") != "SonicSquashfs":
                raise ValueError("missing compression action record")
            if event.get("cacheHit") and event.get("runner") != "disk cache hit":
                raise ValueError("unexpected compression cache strategy")
            receipt = "disk cache hit" if event.get("cacheHit") else "executed locally"
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".squashfs-", delete=False) as tmp:
                pending = Path(tmp.name)
            try:
                shutil.copyfile(workspace / "bazel-bin/root.squashfs", pending)
                os.replace(pending, output)
            finally:
                pending.unlink(missing_ok=True)
            print("SonicSquashfs: " + receipt, flush=True)
            print(f"SquashFS Bazel invocation: {bazel_seconds:.3f}s", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-cache-dir", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--bazel", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.prepare_cache_dir:
            prepare_cache(args.prepare_cache_dir)
        elif all((args.root, args.output, args.cache_dir, args.bazel)):
            compress(args.root, args.output, args.cache_dir, args.bazel)
        else:
            parser.error("specify --root, --output, --cache-dir and --bazel")
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        # Child diagnostics may include private paths or xattr values.
        print(str(error) if isinstance(error, ValueError) else type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
