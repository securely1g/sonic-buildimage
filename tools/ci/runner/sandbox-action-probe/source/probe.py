import errno
import json
import os
from pathlib import Path
import socket
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
assert interfaces == ["lo"], f"unexpected network interfaces: {interfaces}"
result = {"uid": os.getuid(), "guard": guard, "guard_write_errno": denied_errno,
          "parent_netns": parent_netns, "action_netns": netns,
          "interfaces": interfaces, "success": True}
Path(output).write_text(json.dumps(result, sort_keys=True) + "\n")
print(json.dumps(result, sort_keys=True))
