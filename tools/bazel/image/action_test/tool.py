"""Fixture execution tool for action wiring; it does not validate image formats."""
import gzip
import io
from pathlib import Path
import shlex
import sys
import tarfile


args = sys.argv[1:]
if args[0] == "-":  # mksquashfs: retain its input TAR as an inspectable fixture.
    Path(args[1]).write_bytes(sys.stdin.buffer.read())
elif args[0] == "disasm":
    sys.stdin.buffer.read()
    Path(args[args.index("--output") + 1]).write_bytes(gzip.compress(b"[]", mtime=0))
elif args[0] == "-o":  # sgdisk
    pass
elif len(args) == 2 and args[1] == "create":  # grub-editenv
    Path(args[0]).write_bytes(b"fixture grubenv")
elif args[:3] == ["-F", "-t", "ext4"]:
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:
        if not archive.getmembers():
            raise ValueError("empty filesystem fixture")
elif args[:2] == ["-w", "-f"]:  # debugfs write
    command = shlex.split(Path(args[2]).read_text())
    Path(args[-1].split("?", 1)[0] + ".squash-size").write_text(str(Path(command[1]).stat().st_size))
elif args[0] == "-R":  # debugfs stat
    print("Size: " + Path(args[-1].split("?", 1)[0] + ".squash-size").read_text())
elif args[0].startswith("--format="):  # grub-mkimage
    Path(next(value.split("=", 1)[1] for value in args if value.startswith("--output="))).write_bytes(b"fixture GRUB")
elif "--skip-fs-probe" in args:  # grub-bios-setup
    pass
elif args[0] == "convert":  # qemu-img
    Path(args[-1]).write_bytes(b"PR70 action fixture: qcow2\n")
else:
    raise ValueError("unexpected fixture tool arguments: " + repr(args))
