"""Stream selected Spawn metadata from Bazel 8's zstd compact execution log."""

import subprocess


CHUNK = 65536
MAX_VALUE = 16 * 1024 * 1024
MAX_INPUT = 32 * 1024 * 1024 * 1024


class _Reader:
    def __init__(self, stream):
        self.stream = stream
        self.position = 0

    def read(self, size, end, *, eof=False):
        if size < 0 or self.position + size > min(end, MAX_INPUT):
            raise ValueError("Compact execution log exceeds message or reporting limit")
        value = self.stream.read(size)
        self.position += len(value)
        if len(value) != size and not (eof and not value):
            raise ValueError("Truncated compact execution log")
        return value

    def varint(self, end, *, eof=False):
        value = 0
        for index in range(10):
            byte = self.read(1, end, eof=eof and index == 0)
            if not byte:
                return None
            byte = byte[0]
            if index == 9 and byte > 1:
                raise ValueError("Invalid compact execution log varint")
            value |= (byte & 127) << (7 * index)
            if byte < 128:
                return value
        raise ValueError("Invalid compact execution log varint")

    def discard(self, end):
        while self.position < end:
            self.read(min(CHUNK, end - self.position), end)

    def text(self, end):
        if end - self.position > MAX_VALUE:
            raise ValueError("Compact execution metadata exceeds reporting limit")
        return self.read(end - self.position, end).decode("utf-8")

    def fields(self, end):
        while self.position < end:
            key = self.varint(end)
            field, wire = key >> 3, key & 7
            if not 0 < field < 2 ** 29:
                raise ValueError("Invalid compact execution log field")
            if wire == 0:
                yield field, wire, self.varint(end)
            elif wire == 2:
                length = self.varint(end)
                boundary = self.position + length
                if boundary > end:
                    raise ValueError("Truncated compact execution log message")
                yield field, wire, boundary
                self.discard(boundary)
            elif wire in (1, 5):
                self.read(8 if wire == 1 else 4, end)
                yield field, wire, None
            else:
                raise ValueError("Unsupported compact execution log wire type")


def _total_time(reader, end):
    seconds = nanos = 0
    for field, wire, value in reader.fields(end):
        if field in (1, 2):
            if wire != 0:
                raise ValueError("Invalid compact spawn duration")
            if field == 1:
                seconds = value
            else:
                nanos = value
    if seconds >= 2 ** 63 or not 0 <= nanos < 10 ** 9:
        raise ValueError("Invalid compact spawn duration")
    return f"{seconds}.{nanos:09d}s"


def _spawn(reader, end):
    # Field numbers follow src/main/protobuf/spawn.proto at Bazel 8.5.1.
    # File/input-set IDs need not be reconstructed for cache statistics.
    strings = {7: "targetLabel", 8: "mnemonic", 10: "status", 11: "runner"}
    spawn = {"runner": "", "cacheHit": False, "exitCode": 0}
    for field, wire, value in reader.fields(end):
        if field in strings:
            if wire != 2:
                raise ValueError("Invalid compact spawn string")
            spawn[strings[field]] = reader.text(value)
        elif field in (9, 12):
            if wire != 0 or (field == 12 and value not in (0, 1)):
                raise ValueError("Invalid compact spawn count")
            if field == 9:
                spawn["exitCode"] = value if value < 2 ** 63 else value - 2 ** 64
            else:
                spawn["cacheHit"] = bool(value)
        elif field == 18:
            if wire != 2:
                raise ValueError("Invalid compact spawn metrics")
            for metric, metric_wire, boundary in reader.fields(value):
                if metric == 1:
                    if metric_wire != 2:
                        raise ValueError("Invalid compact spawn duration")
                    spawn["metrics"] = {"totalTime": _total_time(reader, boundary)}
    return spawn


def _records(stream):
    reader = _Reader(stream)
    invocation = False
    while True:
        length = reader.varint(MAX_INPUT, eof=True)
        if length is None:
            break
        end = reader.position + length
        kind = None
        spawn = None
        for field, wire, value in reader.fields(end):
            if 2 <= field <= 10:
                if kind is not None or wire != 2:
                    raise ValueError("Invalid compact execution entry")
                kind = field
                if not invocation and kind != 2:
                    raise ValueError("Missing compact execution invocation")
                if kind == 2:
                    if invocation:
                        raise ValueError("Duplicate compact execution invocation")
                    metadata = {}
                    for key, metadata_wire, boundary in reader.fields(value):
                        if key in (1, 4):
                            if metadata_wire != 2:
                                raise ValueError("Invalid compact execution invocation")
                            metadata[key] = reader.text(boundary)
                    if not metadata.get(1) or not metadata.get(4):
                        raise ValueError("Incomplete compact execution invocation")
                    invocation = True
                elif kind == 7:
                    spawn = _spawn(reader, value)
        if kind is None:
            raise ValueError("Missing compact execution entry payload")
        if spawn is not None:
            yield spawn
    if not invocation:
        raise ValueError("Missing compact execution invocation")


def records(path):
    with subprocess.Popen(["zstd", "--decompress", "--stdout", "--quiet", "--", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            yield from _records(process.stdout)
            if process.wait():
                raise ValueError("Invalid or truncated zstd execution log")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
