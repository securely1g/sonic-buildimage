import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys

guard, parent_netns, output = sys.argv[1:]
assert os.getuid() != 0, "action must run as non-root"
assert Path(guard).read_bytes() == b"guard unchanged\n", "outside guard must be visible and unchanged"
try:
    fd = os.open(guard, os.O_WRONLY | os.O_APPEND)
except OSError as exc:
    assert exc.errno == errno.EROFS, f"expected read-only mount, got {exc!r}"
    denied_errno = exc.errno
else:
    os.close(fd)
    raise AssertionError("outside guard remains writable")
netns = os.readlink("/proc/self/ns/net")
assert netns != parent_netns, f"action reused parent network namespace {netns}"
interfaces = sorted(name for _, name in socket.if_nameindex())
# Some kernels create inactive tunnel devices in every network namespace.
# Require no active non-loopback interface and no route to an external address.
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
    interface_flags = {
        name: struct.unpack("16sH14x", fcntl.ioctl(
            control, 0x8913, struct.pack("16sH14x", name.encode(), 0)))[1]
        for name in interfaces
    }
assert not any(flags & 1 and not flags & 8 for flags in interface_flags.values()), interface_flags
routes = Path("/proc/net/route").read_text().strip().splitlines()[1:]
assert not routes, routes
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as external:
    external.settimeout(1)
    try:
        external.connect(("192.0.2.1", 9))
    except OSError as exc:
        assert exc.errno == errno.ENETUNREACH, exc
    else:
        raise AssertionError("unexpected route out of the network namespace")
result = {"uid": os.getuid(), "guard": guard, "guard_write_errno": denied_errno,
          "parent_netns": parent_netns, "action_netns": netns,
          "interfaces": interfaces, "interface_flags": interface_flags,
          "ipv4_routes": routes, "external_connect_errno": errno.ENETUNREACH,
          "success": True}
Path(output).write_text(json.dumps(result, sort_keys=True) + "\n")
print(json.dumps(result, sort_keys=True))
