"""Recover two fixed original VS package files; never construct or execute packages.

Archive decoders are reused unchanged from the qualified original recovery.
Acquisition and current-source admission belong to the calling CI workflow.
"""
import argparse
import binascii
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import zipfile

CHUNK = 1024 * 1024
GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_BIN = 8 * GIB
MAX_PLATFORM = 256 * MIB
MAX_EXPANDED_PLATFORM = 512 * MIB
MAX_DEB = 128 * MIB
MAX_MEMBERS = 4096
MAX_TAIL = MIB
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

class RecoveryError(ValueError):
    pass

def require(condition, reason):
    if not condition:
        raise RecoveryError(reason)

def stable(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)

class HeldFile:
    """Hold every directory, then open one regular basename without following links."""
    def __init__(self, path):
        path = Path(path)
        require(path.is_absolute() and str(path) == os.path.normpath(str(path)),
                "Input path is not an absolute normalized path")
        self.path, self.fds, self.edges = path, [], []
        try:
            parent = os.open("/", DIRECTORY_FLAGS)
            self.fds.append(parent)
            for part in path.parts[1:-1]:
                named = os.stat(part, dir_fd=parent, follow_symlinks=False)
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=parent)
                self.fds.append(child)
                opened = os.fstat(child)
                require((named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino),
                        "Input directory changed while opening")
                self.edges.append((parent, part, child, (opened.st_dev, opened.st_ino)))
                parent = child
            self.parent = parent
            self.fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=parent)
            self.fds.append(self.fd)
            self.initial = os.fstat(self.fd)
            require(stat.S_ISREG(self.initial.st_mode), "Input is not a regular file")
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self):
        for parent, name, child, identity in self.edges:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            require(stat.S_ISDIR(named.st_mode) and
                    (named.st_dev, named.st_ino) == identity == (opened.st_dev, opened.st_ino),
                    "Input directory identity changed")
        named = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
        require(stable(named) == stable(os.fstat(self.fd)) == stable(self.initial),
                "Input file identity or metadata changed")

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

def hash_range(fd, offset, size, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    count = 0
    while count < size:
        chunk = os.pread(fd, min(CHUNK, size - count), offset + count)
        require(chunk, "Truncated original byte range")
        digest.update(chunk)
        count += len(chunk)
    return digest.hexdigest()

def clean_name(name, directory=False, tar=False):
    require(type(name) is str and 0 < len(name) <= 1024 and
            not name.startswith("/") and "\\" not in name and
            not any(ord(c) < 32 or ord(c) == 127 for c in name), "Unsafe member name")
    value = name[:-1] if directory and name.endswith("/") else name
    if tar and value.startswith("./"):
        value = value[2:]
    if tar and directory and value in ("", "."):
        return "."
    require(all(part not in ("", ".", "..") and ":" not in part
                for part in value.split("/")), "Unsafe member components")
    return value

def zip_inventory(archive, expanded_limit, per_member_limit, expected_names=None):
    rows = archive.infolist()
    require(0 < len(rows) <= MAX_MEMBERS, "ZIP member count exceeds bound")
    require(sum(v.file_size for v in rows) <= expanded_limit, "ZIP expanded size exceeds bound")
    seen, regular, result = set(), set(), []
    for item in rows:
        require(item.filename == item.orig_filename, "ZIP filename truncation")
        name = clean_name(item.filename, item.is_dir())
        require(name not in seen, "Duplicate ZIP member")
        seen.add(name)
        kind = stat.S_IFMT(item.external_attr >> 16)
        require(kind in ((0, stat.S_IFDIR) if item.is_dir() else (0, stat.S_IFREG)),
                "ZIP member is not a regular file/directory")
        require(not item.flag_bits & 1 and item.compress_type in
                (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "Unsupported ZIP encoding")
        require(0 <= item.file_size <= per_member_limit and
                (not item.is_dir() or item.file_size == 0), "ZIP member size exceeds bound")
        if not item.is_dir():
            regular.add(name)
        result.append({"name": item.filename, "directory": item.is_dir(),
                       "bytes": item.file_size, "compressed_bytes": item.compress_size,
                       "CRC32": format(item.CRC, "08x"), "compression": item.compress_type})
    for name in seen:
        parts = name.split("/")
        require(all("/".join(parts[:i]) not in regular for i in range(1, len(parts))),
                "ZIP member crosses a regular-file parent")
    if expected_names is not None:
        require(not any(v.is_dir() for v in rows) and regular == set(expected_names),
                "Original artifact member set differs from the recorded upload")
    return result

def copy_member(source, target, expected_size, maximum):
    require(0 <= expected_size <= maximum, "Selected member exceeds its bound")
    count, crc, digest = 0, 0, hashlib.sha256()
    while True:
        chunk = source.read(min(CHUNK, expected_size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        require(count <= expected_size, "Selected member exceeds declared size")
        target.write(chunk)
        digest.update(chunk)
        crc = binascii.crc32(chunk, crc)
    require(count == expected_size, "Selected member is truncated")
    target.flush()
    os.fsync(target.fileno())
    return {"bytes": count, "sha256": digest.hexdigest(), "CRC32": format(crc & 0xffffffff, "08x")}

def header_boundary(fd, bin_size, template):
    require(template.endswith(b"\nexit_marker\n") and
            template.count(b"%%PAYLOAD_IMAGE_SIZE%%") == template.count(b"%%IMAGE_SHA1%%") == 1,
            "Reviewed wrapper template differs")
    prefix = os.pread(fd, len(template) + 128, 0)
    marker_end = prefix.find(b"\nexit_marker\n")
    require(marker_end >= 0, "Literal installer boundary is absent")
    prefix = prefix[:marker_end + len(b"\nexit_marker\n")]
    sizes = re.findall(rb"(?m)^payload_image_size=([1-9][0-9]{0,19})$", prefix)
    hashes = re.findall(rb"(?m)^payload_sha1=([0-9a-f]{40})$", prefix)
    require(len(sizes) == len(hashes) == 1, "Installer header fields differ")
    size = int(sizes[0])
    header = template.replace(b"%%PAYLOAD_IMAGE_SIZE%%", sizes[0]).replace(b"%%IMAGE_SHA1%%", hashes[0])
    require(prefix == header and 1024 <= size <= MAX_BIN,
            "Installer header is not the exact two-substitution template")
    require(len(header) + size == bin_size,
            "Unexpected appended signature or bytes; preserve and review the unsigned producer contract")
    require(hash_range(fd, len(header), size, "sha1") == hashes[0].decode(),
            "Appended TAR SHA1 differs from original header")
    return {"offset": len(header), "bytes": size, "sha1": hashes[0].decode(),
            "header_sha256": hashlib.sha256(header).hexdigest(), "trailing_bytes": 0,
            "unsigned_original_profile_checked": True}

class BoundedTarInfo(tarfile.TarInfo):
    """Use the stdlib header decoder, bounding GNU metadata before it allocates."""
    @classmethod
    def fromtarfile(cls, archive):
        try:
            return super().fromtarfile(archive)
        except tarfile.EOFHeaderError:
            archive._recovery_zero_end = True
            raise
        except tarfile.HeaderError as error:
            raise RecoveryError("Malformed TAR header") from error

    def _proc_member(self, archive):
        archive._recovery_headers = getattr(archive, "_recovery_headers", 0) + 1
        require(archive._recovery_headers <= MAX_MEMBERS * 3, "TAR header count exceeds bound")
        require(self.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE,
                              tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.GNUTYPE_LONGNAME,
                              tarfile.GNUTYPE_LONGLINK), "Unsupported TAR type/PAX/sparse metadata")
        require(0 <= self.size <= MAX_BIN, "TAR member exceeds bound")
        if self.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            require(self.size <= 64 * 1024, "GNU TAR metadata exceeds bound")
        return super()._proc_member(archive)

def tar_record(item, seen):
    name = clean_name(item.name, item.isdir(), tar=True)
    require(name not in seen and not item.issparse(), "Duplicate or sparse TAR member")
    seen.add(name)
    require(len(seen) <= MAX_MEMBERS, "TAR member count exceeds bound")
    return {"declared_name": item.name, "normalized_name": name,
            "type": item.type.decode("ascii"), "bytes": item.size, "offset_data": item.offset_data}

def zero_tail(stream, maximum=MAX_TAIL):
    count = 0
    while True:
        chunk = stream.read(min(CHUNK, maximum - count + 1))
        if not chunk:
            break
        count += len(chunk)
        require(count <= maximum and not chunk.strip(b"\0"),
                "Nonzero or excessive TAR tail; concatenated archives are not allowed")
    require(count >= 512, "TAR lacks its second zero end block")
    return count

class Meter:
    """Forward-only bounded byte/hash observation around an upstream stream."""
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.count = stream, maximum, 0
        self.digest = hashlib.sha256()
        self.crc = 0

    def read(self, size=-1):
        require(size >= 0, "Unbounded stream read is prohibited")
        data = self.stream.read(min(size, CHUNK, self.maximum - self.count + 1))
        self.count += len(data)
        require(self.count <= self.maximum, "Stream expanded size exceeds bound")
        self.digest.update(data)
        self.crc = binascii.crc32(data, self.crc)
        return data

    def tell(self):
        return self.count

class Outputs:
    def __init__(self, parent, parent_fd):
        self.path, self.parent_fd, self.files = parent / "execution", parent_fd, {}
        os.mkdir("execution", 0o700, dir_fd=parent_fd)
        self.fd = os.open("execution", DIRECTORY_FLAGS, dir_fd=parent_fd)
        self.identity = os.fstat(self.fd)

    @contextlib.contextmanager
    def open(self, name, binary=True):
        require(name and "/" not in name and name not in (".", ".."), "Unsafe fixed output name")
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        with os.fdopen(fd, "w+b" if binary else "w+", **({} if binary else {"encoding": "utf-8"})) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
            saved = stable(os.fstat(stream.fileno()))
            require(stable(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) == saved,
                    "Output file changed while retained")
            self.files[name] = saved

    def record(self, name, value):
        with self.open(name, False) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")

    def verify(self):
        named = os.stat("execution", dir_fd=self.parent_fd, follow_symlinks=False)
        require(stat.S_ISDIR(named.st_mode) and (named.st_dev, named.st_ino) ==
                (self.identity.st_dev, self.identity.st_ino), "Output directory changed")
        for name, saved in self.files.items():
            require(stable(os.stat(name, dir_fd=self.fd, follow_symlinks=False)) == saved,
                    "Retained output metadata changed")

    def close(self):
        os.close(self.fd)

def selected_platform(bin_file, boundary, output):
    bin_file.seek(boundary["offset"])
    outer_rows, outer_seen, fs_member = [], set(), None
    with tarfile.open(fileobj=bin_file, mode="r:", tarinfo=BoundedTarInfo) as outer:
        for item in outer:
            row = tar_record(item, outer_seen)
            outer_rows.append(row)
            require(item.offset_data >= boundary["offset"] and item.offset_data + item.size <=
                    boundary["offset"] + boundary["bytes"], "Outer TAR member escapes its verified payload")
            if row["normalized_name"] == "installer/fs.zip":
                require(item.isreg() and 0 < item.size <= MAX_BIN, "Installer payload is not a regular ZIP")
                fs_member = item
        require(fs_member is not None and getattr(outer, "_recovery_zero_end", False),
                "Missing installer/fs.zip or complete TAR terminator")
        end = outer.offset
        remaining = boundary["offset"] + boundary["bytes"] - end
        require(1024 <= remaining <= MAX_TAIL, "Unexpected outer TAR trailing size")
        bin_file.seek(end)
        tail = bin_file.read(remaining)
        require(len(tail) == remaining and not tail.strip(b"\0"), "Unexpected outer TAR trailing bytes")
        output.record("outer-tar-inventory.json", {"members": outer_rows, "zero_tail_bytes": remaining})
        with outer.extractfile(fs_member) as fs_stream:
            require(fs_stream.seekable(), "Standard TAR member stream is not seekable")
            with zipfile.ZipFile(fs_stream) as filesystem:
                inner_rows = zip_inventory(filesystem, MAX_BIN, MAX_BIN)
                selected = [x for x in filesystem.infolist() if x.filename == "platform.tar.gz"]
                require(len(selected) == 1 and not selected[0].is_dir(), "Missing unique platform.tar.gz")
                member = selected[0]
                require(member.compress_type == zipfile.ZIP_STORED and 0 < member.file_size <= MAX_PLATFORM,
                        "Platform member differs from the source zip -n .gz contract")
                output.record("fs-zip-inventory.json", {"members": inner_rows,
                              "unselected_member_streams_opened": 0})
                rows, seen, package = [], set(), None
                with filesystem.open(member) as compressed:
                    compressed_meter = Meter(compressed, MAX_PLATFORM)
                    with gzip.GzipFile(fileobj=compressed_meter, mode="rb") as inflated:
                        expanded_meter = Meter(inflated, MAX_EXPANDED_PLATFORM)
                        with tarfile.open(fileobj=expanded_meter, mode="r|", tarinfo=BoundedTarInfo) as platform:
                            for item in platform:
                                row = tar_record(item, seen)
                                rows.append(row)
                                if row["normalized_name"] == "common/sonic-platform-vs_1.0_amd64.deb":
                                    require(item.isreg() and 0 < item.size <= MAX_DEB,
                                            "Selected platform package is not a bounded regular member")
                                    with platform.extractfile(item) as package_stream, output.open("sonic-platform-vs_1.0_amd64.deb") as target:
                                        package = copy_member(package_stream, target, item.size, MAX_DEB)
                                    package.update(declared_member=item.name, normalized_member=row["normalized_name"])
                            require(getattr(platform, "_recovery_zero_end", False), "Missing platform TAR terminator")
                            tail_bytes = zero_tail(platform.fileobj)
                        require(expanded_meter.read(1) == b"", "Unconsumed gzip payload")
                    require(compressed_meter.read(1) == b"" and compressed_meter.count == member.file_size,
                            "Incomplete original platform gzip member")
                    require(format(compressed_meter.crc & 0xffffffff, "08x") == format(member.CRC, "08x"),
                            "Original platform ZIP CRC differs")
                require(package is not None, "Original common platform DEB is absent")
                output.record("platform-tar-inventory.json", {"members": rows, "zero_tail_bytes_after_first_end_block": tail_bytes,
                              "uncompressed_bytes_observed": expanded_meter.count,
                              "gzip_trailer_checked": True, "selected_original_member_only": True})
                return {"platform_member": {"bytes": compressed_meter.count,
                        "sha256": compressed_meter.digest.hexdigest(), "CRC32": format(member.CRC, "08x"),
                        "gzip_trailer_checked": True, "original_zip_member_CRC_checked": True},
                        "original_package": package}

PLATFORM = {
    "bytes": 2562070511,
    "sha256": "b3e9ab8c562aefc6bc3b5038ff8c978b4cabeaff00e003e09be882d048c40980",
    "artifact": 13630197696, "run": 42504707668, "job": 128805164461,
    "producer": "15c277f89f12bacd240e160c555263e90d2179fc",
    "member": "target/sonic-vs.bin",
    "package": "sonic-platform-vs_1.0_amd64.deb",
    "package_bytes": 4852,
    "package_sha256": "3c0ff1281d14ecf6173ebc301dda1e4544dc326048a7a4453413d37217372036",
}
WRAPPER = {
    "bytes": 2017,
    "sha256": "a9f3521062d967cd4ae1b5715e43e95504d1ce732dd57d747a4bbee1dd2898af",
    "member": "installer/sharch_body.sh",
}
DEVICE = {
    "bytes": 16302731,
    "sha256": "2f3f982d43fc34373e04a5e9f22e765f1b98015b7aa4896d26d61e2072cc1fc8",
    "artifact": 13821942674, "run": 42945002504, "job": 130275576933,
    "producer": "d8186092711a0385755fffe9ff229632231df4ef",
    "member": "layer/stdout.data",
    "expanded_bytes": 16311808,
    "diff_id": "5943c6099464eaa84cd409fce40b78a90d0fe284585c442c4d767021cddf156f",
    "package": "sonic-device-data_1.0-1_all.deb",
    "package_bytes": 16309700,
    "package_sha256": "9a35910de3c9e0d10e3259098d16846798bba5cd7ccfa7c3c4bdc2a1e88fd751",
}


@contextlib.contextmanager
def output_parent(path):
    """Hold an existing private destination through each directory component."""
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)),
            "Output parent is not an absolute normalized path")
    fds, edges = [], []
    try:
        fd = os.open("/", DIRECTORY_FLAGS)
        fds.append(fd)
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            fds.append(child)
            value = os.fstat(child)
            named = os.stat(part, dir_fd=fd, follow_symlinks=False)
            require(stat.S_ISDIR(named.st_mode) and
                    (named.st_dev, named.st_ino) == (value.st_dev, value.st_ino),
                    "Output parent changed while opening")
            edges.append((fd, part, child, (value.st_dev, value.st_ino)))
            fd = child
        value = os.fstat(fd)
        require(value.st_uid == os.getuid() and not value.st_mode & 0o077,
                "Output parent must be private and owned by the current user")
        yield fd
        for parent, name, child, identity in edges:
            named, opened = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(child)
            require(stat.S_ISDIR(named.st_mode) and
                    (named.st_dev, named.st_ino) == identity == (opened.st_dev, opened.st_ino),
                    "Output parent identity changed")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def verify_original(held, expected):
    require(held.initial.st_size == expected["bytes"] and
            hash_range(held.fd, 0, held.initial.st_size) == expected["sha256"],
            "Original input byte identity differs")
    held.verify()


def recover_device(held, output, expected):
    """Copy a known original member; use the same bounded stdlib TAR readers."""
    rows, seen, package = [], set(), None
    with os.fdopen(os.dup(held.fd), "rb") as source:
        compressed = Meter(source, expected["bytes"])
        with gzip.GzipFile(fileobj=compressed, mode="rb") as inflated:
            expanded = Meter(inflated, expected["expanded_bytes"])
            with tarfile.open(fileobj=expanded, mode="r|", tarinfo=BoundedTarInfo) as archive:
                for item in archive:
                    row = tar_record(item, seen)
                    rows.append(row)
                    require(item.uid == item.gid == 0 and not item.pax_headers,
                            "Original layer ownership or metadata differs")
                    if row["normalized_name"] == "debs":
                        require(item.isdir() and item.size == 0 and item.mode == 0o755,
                                "Original package directory differs")
                    else:
                        require(row["normalized_name"] == "debs/" + expected["package"] and
                                item.isreg() and item.mode == 0o644 and
                                item.size == expected["package_bytes"],
                                "Unexpected original COPY-layer member")
                        with archive.extractfile(item) as member, output.open(expected["package"]) as target:
                            package = copy_member(member, target, item.size, MAX_DEB)
                require(seen == {"debs", "debs/" + expected["package"]} and
                        getattr(archive, "_recovery_zero_end", False),
                        "Incomplete original COPY-layer members or terminator")
                tail = zero_tail(archive.fileobj)
            require(expanded.read(1) == b"" and expanded.count == expected["expanded_bytes"] and
                    expanded.digest.hexdigest() == expected["diff_id"],
                    "Original COPY-layer expanded identity differs")
        require(compressed.read(1) == b"" and compressed.count == expected["bytes"] and
                compressed.digest.hexdigest() == expected["sha256"],
                "Original COPY-layer compressed identity differs")
    require(package is not None and package["sha256"] == expected["package_sha256"],
            "Original device package identity differs")
    output.record("device-layer-inventory.json", {
        "members": rows, "zero_tail_bytes": tail, "expanded_bytes": expanded.count,
        "diff_id": expanded.digest.hexdigest(), "gzip_trailer_checked": True,
    })
    return {"original_package": package,
            "nested_member": "debs/" + expected["package"]}


def recover(kind, input_path, parent, wrapper=None):
    expected = PLATFORM if kind == "platform" else DEVICE
    require(kind in ("platform", "device-data"), "Unknown recovery kind")
    require((kind == "platform") == (wrapper is not None),
            "Only platform recovery requires its reviewed wrapper template")
    with contextlib.ExitStack() as stack:
        original = stack.enter_context(HeldFile(input_path))
        verify_original(original, expected)
        template = None
        if wrapper is not None:
            template_file = stack.enter_context(HeldFile(wrapper))
            verify_original(template_file, WRAPPER)
            template = os.pread(template_file.fd, WRAPPER["bytes"], 0)
        parent_fd = stack.enter_context(output_parent(parent))
        space = os.fstatvfs(parent_fd)
        require(space.f_bavail * space.f_frsize >= expected["package_bytes"] + MIB,
                "Insufficient space for recovered original bytes and metadata")
        output = Outputs(Path(parent), parent_fd)
        stack.callback(output.close)
        if kind == "platform":
            boundary = header_boundary(original.fd, original.initial.st_size, template)
            with os.fdopen(os.dup(original.fd), "rb") as stream:
                result = selected_platform(stream, boundary, output)
            result["payload_boundary"] = boundary
            template_file.verify()
        else:
            result = recover_device(original, output, expected)
        package = result["original_package"]
        require(package["bytes"] == expected["package_bytes"] and
                package["sha256"] == expected["package_sha256"],
                "Recovered original package identity differs")
        original.verify()
        output.verify()
        result.update(
            status="ORIGINAL_INPUT_BYTES_RECOVERED_NOT_ADMITTED", kind=kind,
            original_source_reference={k: expected[k] for k in ("artifact", "run", "job", "producer", "member")},
            original_input={k: expected[k] for k in ("bytes", "sha256")},
            original_inputs_unchanged=True,
            installer_execution=False, package_generation=False, package_installation=False,
            transport_validation_performed_by_this_tool=False,
            current_source_or_full_vs_admission=False,
        )
        output.record("recovered-original-input.json", result)
        output.verify()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("platform", "device-data"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-parent", required=True, type=Path,
                        help="Existing private directory; execution/ must not exist")
    parser.add_argument("--wrapper", type=Path,
                        help="The reviewed installer/sharch_body.sh; read only")
    args = parser.parse_args()
    print(json.dumps(recover(args.kind, args.input, args.output_parent, args.wrapper),
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
