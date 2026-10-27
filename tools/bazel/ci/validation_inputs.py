"""Reuse verified immutable inputs within one validation, never across builds.

Only content hashes and parsed inventories are retained. Validation policies,
overlay operations and ELF/debug inspection still run for every consumer.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
from pathlib import Path
import sys

_current = ContextVar("validation_inputs", default=None)


def _stamp(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode)


def _unchanged(path, before):
    if _stamp(path) != before:
        raise ValueError("validation input changed: " + str(path))


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _size(value):
    """Conservative retained-size bound for plain inventory containers."""
    result = sys.getsizeof(value)
    if isinstance(value, dict):
        result += sum(_size(key) + _size(item) for key, item in value.items())
    elif isinstance(value, (tuple, list)):
        result += sum(_size(item) for item in value)
    return result


class _Inputs:
    def __init__(self, paths, max_bytes, max_files):
        self.paths = [(Path(path).absolute(), Path(path).is_dir()) for path in paths]
        self.max_bytes, self.max_files = max_bytes, max_files
        self.files, self.inventories = {}, OrderedDict()
        self.bytes = 0

    def contains(self, path):
        path = Path(path).absolute()
        return any(path == root or (directory and path.is_relative_to(root))
                   for root, directory in self.paths)

    def digest(self, path):
        path = Path(path).absolute()
        before = _stamp(path)
        if path in self.files:
            stamp, digest = self.files[path]
            _unchanged(path, stamp)
            return digest
        digest = _digest(path)
        _unchanged(path, before)
        if len(self.files) < self.max_files:
            self.files[path] = (before, digest)
        return digest

    def inventory(self, path, kind, read):
        path = Path(path).absolute()
        before = _stamp(path)
        digest = self.digest(path)
        if path not in self.files:
            # Do not retain a result whose path cannot be checked at scope exit.
            result = read()
            _unchanged(path, before)
            return result
        key = (kind, digest)
        if key in self.inventories:
            self.inventories.move_to_end(key)
            result = deepcopy(self.inventories[key][0])
        else:
            result = read()
            _unchanged(path, before)
            size = _size(result)
            if size <= self.max_bytes:
                while self.inventories and self.bytes + size > self.max_bytes:
                    _, (_, removed) = self.inventories.popitem(last=False)
                    self.bytes -= removed
                self.inventories[key] = (deepcopy(result), size)
                self.bytes += size
        _unchanged(path, before)
        return result

    def verify(self):
        for path, (stamp, digest) in self.files.items():
            _unchanged(path, stamp)
            if _digest(path) != digest:
                raise ValueError("validation input changed: " + str(path))
            _unchanged(path, stamp)


@contextmanager
def validation_input_cache(paths, *, max_bytes=64 * 1024 * 1024, max_files=512):
    """Share successful reads until scope exit; reject observed input mutation.

    Only the declared paths (and blobs below declared OCI directories) qualify;
    temporary extracted ELFs and other generated files are never retained.
    Each path is SHA256-verified before content-keyed reuse. Stat identities
    (including ctime, inode and device) are checked around reads and at hits;
    successful exit rechecks both identity and bytes before publishing results.
    These must be read-only validation inputs, not concurrent build outputs.
    Oversized inventories and paths beyond the file budget are
    read normally instead of retained.
    """
    inputs = _Inputs(paths, max_bytes, max_files)
    token = _current.set(inputs)
    try:
        yield
        inputs.verify()
    finally:
        _current.reset(token)


def sha256(path):
    inputs = _current.get()
    return inputs.digest(path) if inputs is not None and inputs.contains(path) else _digest(path)


def cached_inventory(path, kind, read):
    inputs = _current.get()
    return inputs.inventory(path, kind, read) if inputs is not None and inputs.contains(path) else read()
