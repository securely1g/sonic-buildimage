#!/usr/bin/env python3
"""Collect native Docker 28 overlay2 layers into reusable compressed tar pieces.

The daemon must be stopped before collection. Docker performs archive extraction;
this tool only gives its immutable layers stable names and packages their files.
No Docker socket is accessed. A part contains one gzip member per ChainID, each
holding a tar fragment WITHOUT end-of-archive blocks. Merge concatenates members,
adds image/tag metadata, and appends a single tar terminator. The result is an
ordinary gzip-compressed tar understood by SONiC's existing installer. Merge
lowers opaque directories to ordinary whiteouts before packaging, because the
current SONiC installer does not restore xattrs. Other semantic xattrs fail closed.

This is deliberately specific to Docker's classic overlay2 image store. It is
not a converter for containerd's image store or an undocumented universal format.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gzip
import hashlib
import io
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

HEX = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SCHEMA = 1


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def validate_image(config):
    settings = config.get("config") or {}
    labels = settings.get("Labels") or {}
    require(labels.get("org.sonic.bazel.host-metadata-only") != "true",
            "refusing to ship a host-metadata-only staging image")


def chain_ids(diff_ids):
    result = []
    for diff in diff_ids:
        require(isinstance(diff, str) and DIGEST.fullmatch(diff), "invalid layer DiffID")
        result.append(diff if not result else "sha256:" + hashlib.sha256(
            (result[-1] + " " + diff).encode()).hexdigest())
    require(len(result) <= 125, "Docker layer depth exceeds 125")
    return result


def link_id(chain):
    return base64.b32encode(bytes.fromhex(chain.removeprefix("sha256:"))).decode()[:26]


def regular(path):
    require(stat.S_ISREG(path.lstat().st_mode), "expected regular file: " + str(path))
    return path.read_bytes()


def directory(path):
    require(stat.S_ISDIR(path.lstat().st_mode), "expected directory: " + str(path))


@contextmanager
def compressed(path, pigz=None, level=6):
    with open(path, "wb") as raw:
        if pigz:
            process = subprocess.Popen([pigz, "-n", "-p", "1", "-" + str(level), "-c"],
                                       stdin=subprocess.PIPE, stdout=raw)
            try:
                yield process.stdin
            finally:
                process.stdin.close()
                rc = process.wait()
                require(rc == 0, "pigz failed with status " + str(rc))
        else:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                               compresslevel=level, mtime=0) as stream:
                yield stream


class CountingWriter:
    """Give tarfile an offset without seeking a compressor pipe."""
    def __init__(self, stream):
        self.stream = stream
        self.offset = 0

    def write(self, data):
        count = self.stream.write(data)
        self.offset += count
        return count

    def tell(self):
        return self.offset


@contextmanager
def fragment(path, pigz=None, level=6):
    with compressed(path, pigz, level) as output:
        archive = tarfile.open(fileobj=CountingWriter(output), mode="w", format=tarfile.PAX_FORMAT)
        try:
            yield archive
        finally:
            # TarFile.close appends EOF blocks and record padding. Fragments must
            # stop on the next header boundary so gzip-member concatenation works.
            archive.closed = True


def add_bytes(archive, name, data, mode=0o644):
    item = tarfile.TarInfo(name)
    item.size = len(data)
    item.mode = mode
    archive.addfile(item, io.BytesIO(data))


def add_dir(archive, name, mode=0o755):
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE
    item.mode = mode
    archive.addfile(item)


def add_link(archive, name, target):
    item = tarfile.TarInfo(name)
    item.type = tarfile.SYMTYPE
    item.linkname = target
    item.mode = 0o777
    archive.addfile(item)


def is_directory(path):
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def parent_directory(root, relative):
    """Find a corresponding lower directory without following any symlink."""
    current = root
    for component in relative.parts:
        current = current / component
        if not is_directory(current):
            return None
    return current


def opaque_whiteouts(upper, lower, relative, result):
    """Express an opaque upper directory using missing-child whiteouts.

    `lower` is the kernel-resolved parent view, not a hand-merged approximation.
    Recurse only through directories that actually exist in the upper layer:
    a single whiteout hides a missing lower subtree, and a non-directory upper
    entry already hides a corresponding lower directory. Never follow symlinks.
    """
    if lower is None or not is_directory(lower):
        return
    for child in sorted(lower.iterdir()):
        destination = upper / child.name
        name = relative / child.name
        try:
            destination.lstat()
        except FileNotFoundError:
            result.add(str(name))
            continue
        if is_directory(destination) and is_directory(child):
            opaque_whiteouts(destination, child, name, result)


class ParentView:
    """Lazily mount a native layer's ancestors read-only in the worker namespace."""
    def __init__(self, overlay_root, source):
        self.overlay_root = overlay_root
        self.source = source
        self.temp = None
        self.path = None
        self.mounted = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self):
        if self.path is not None:
            return self.path
        self.temp = Path(tempfile.mkdtemp(prefix="sonic-parent-"))
        self.path = self.temp / "merged"
        self.path.mkdir()
        lower_file = self.source / "lower"
        if not lower_file.exists():
            return self.path  # Base layer has an empty parent filesystem.
        lower = regular(lower_file).decode()
        require(re.fullmatch(r"l/[A-Z0-9]{26}(:l/[A-Z0-9]{26})*", lower), "invalid parent lower references")
        empty = self.temp / "empty"
        empty.mkdir()
        # Relative 26-character native links keep the mount option below the
        # kernel's limit even with a long Bazel output root and deep images.
        # Like Docker's overlay2 driver, use mount(2). util-linux's newer
        # fsopen/fsconfig API is not supported by every worker kernel/runtime.
        # A child process supplies cwd without changing it in this multithreaded
        # collector. Flags are MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC.
        program = """import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
if libc.mount(b'overlay', os.fsencode(sys.argv[1]), b'overlay', 15, os.fsencode(sys.argv[2])):
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code))
"""
        subprocess.run([sys.executable, "-c", program, str(self.path),
                        "lowerdir=" + lower + ":" + str(empty)], cwd=self.overlay_root, check=True)
        self.mounted = True
        return self.path

    def close(self):
        if self.mounted:
            subprocess.run(["umount", str(self.path)], check=True)
            self.mounted = False
        if self.path is not None:
            self.path.rmdir()
            self.path = None
        if self.temp is not None:
            if (self.temp / "empty").exists():
                (self.temp / "empty").rmdir()
            self.temp.rmdir()
            self.temp = None


def add_native_tree(archive, source, target, root=True, observed=None,
                    parent_view=None, diff_root=None, whiteouts=None):
    """Preserve hard links, ownership, devices, file times and binary xattrs."""
    item = archive.gettarinfo(str(source), arcname=target)
    item.uname = item.gname = ""
    whiteout = item.ischr() and item.devmajor == item.devminor == 0
    if observed is not None and whiteout:
        observed["native_whiteouts"] += 1
    if root or whiteout:
        # Native import creates the diff root and whiteouts at wall clock time.
        item.mtime = 0
    for attr in sorted(os.listxattr(source, follow_symlinks=False)):
        value = os.getxattr(source, attr, follow_symlinks=False)
        if observed is not None:
            observed["native_xattrs"][attr] = observed["native_xattrs"].get(attr, 0) + 1
        if attr == "trusted.overlay.opaque" and item.isdir() and value in (b"y", b"x"):
            # Linux overlayfs documents 'y' as directory opacity and 'x' as a
            # readdir optimization for xattr-based whiteouts. The latter are
            # still rejected below; dropping 'x' alone changes no visible names.
            # https://docs.kernel.org/filesystems/overlayfs.html#whiteouts-and-opaque-directories
            require(parent_view is not None, "opaque lowering requires a native parent view")
            if value == b"y":
                relative = source.relative_to(diff_root)
                opaque_whiteouts(source, parent_directory(parent_view.get(), relative), relative, whiteouts)
                observed["lowered_opaque_directories"].append(str(relative))
            else:
                observed["omitted_opaque_optimization"] += 1
            continue
        if observed is not None:
            observed["archive_xattrs"][attr] = observed["archive_xattrs"].get(attr, 0) + 1
        item.pax_headers["SCHILY.xattr." + attr] = value.decode("utf-8", "surrogateescape")
    if item.isreg():
        with source.open("rb") as data:
            archive.addfile(item, data)
    else:
        archive.addfile(item)
    if item.isdir():
        for child in sorted(source.iterdir()):
            add_native_tree(archive, child, target + "/" + child.name, root=False, observed=observed,
                            parent_view=parent_view, diff_root=diff_root, whiteouts=whiteouts)


def collect(store_root, output, tags=(), pigz=None, jobs=1, level=6):
    root, output = Path(store_root), Path(output)
    require(not output.exists(), "output part already exists: " + str(output))
    layer_root = root / "image/overlay2/layerdb/sha256"
    image_root = root / "image/overlay2/imagedb/content/sha256"
    directory(layer_root)
    directory(image_root)
    repos = json.loads(regular(root / "image/overlay2/repositories.json"))
    selected = {}
    for repo, refs in repos.get("Repositories", {}).items():
        for ref, image_id in refs.items():
            if not tags or ref in tags:
                require(isinstance(image_id, str) and DIGEST.fullmatch(image_id), "invalid image ID")
                selected[ref] = {"repository": repo, "image_id": image_id}
    require(selected, "no selected image references")
    require(not tags or set(tags) == set(selected), "one or more requested references are missing")
    images, layers = {}, {}
    for image_id in sorted({value["image_id"] for value in selected.values()}):
        data = regular(image_root / image_id[7:])
        require(hashlib.sha256(data).hexdigest() == image_id[7:], "image config digest mismatch")
        config = json.loads(data)
        validate_image(config)
        require(config.get("rootfs", {}).get("type") == "layers", "unsupported image rootfs")
        diffs = config["rootfs"]["diff_ids"]
        chains = chain_ids(diffs)
        images[image_id] = {"config": base64.b64encode(data).decode(), "chains": chains}
        for index, (chain, diff) in enumerate(zip(chains, diffs)):
            if chain in layers:
                continue
            folder = layer_root / chain[7:]
            directory(folder)
            actual_diff = regular(folder / "diff").decode()
            require(actual_diff == diff, "native layer DiffID mismatch: " + chain)
            parent = regular(folder / "parent").decode() if (folder / "parent").exists() else ""
            require(parent == (chains[index - 1] if index else ""), "native parent mismatch: " + chain)
            native_cache = regular(folder / "cache-id").decode()
            require(HEX.fullmatch(native_cache), "invalid native cache ID")
            overlay = root / "overlay2" / native_cache
            directory(overlay)
            directory(overlay / "diff")
            native_link = regular(overlay / "link").decode()
            require(re.fullmatch(r"[A-Z0-9]{26}", native_link), "invalid native link ID")
            require(os.readlink(root / "overlay2/l" / native_link) == "../" + native_cache + "/diff",
                    "native overlay link mismatch")
            expected_lower = []
            for earlier in reversed(chains[:index]):
                earlier_cache = regular(layer_root / earlier[7:] / "cache-id").decode()
                earlier_link = regular(root / "overlay2" / earlier_cache / "link").decode()
                expected_lower.append("l/" + earlier_link)
            actual_lower = regular(overlay / "lower").decode() if (overlay / "lower").exists() else ""
            require(actual_lower == ":".join(expected_lower), "native lower-order mismatch: " + chain)
            size = regular(folder / "size").decode()
            require(size.isdigit(), "invalid native layer size")
            require((folder / "tar-split.json.gz").is_file(), "missing native tar-split metadata")
            layers[chain] = {"diff": diff, "parent": parent, "size": int(size),
                             "ancestors": chains[:index], "native_cache": native_cache}
    output.mkdir(parents=True)
    (output / "layers").mkdir()

    def package(item):
        chain, value = item
        cache = chain[7:]
        prefix = "overlay2/" + cache
        source = root / "overlay2" / value["native_cache"]
        native_db = layer_root / chain[7:]
        target_db = "image/overlay2/layerdb/sha256/" + chain[7:]
        path = output / "layers" / (chain[7:] + ".tar.gz")
        observed = {"native_whiteouts": 0, "native_xattrs": {}, "archive_xattrs": {},
                    "lowered_opaque_directories": [], "omitted_opaque_optimization": 0}
        whiteouts = set()
        with fragment(path, pigz, level) as archive, ParentView(root / "overlay2", source) as parent_view:
            add_dir(archive, target_db, 0o700)
            for name, data in (("diff", value["diff"]), ("cache-id", cache), ("size", str(value["size"]))):
                add_bytes(archive, target_db + "/" + name, data.encode())
            if value["parent"]:
                add_bytes(archive, target_db + "/parent", value["parent"].encode())
            split = gzip.decompress(regular(native_db / "tar-split.json.gz"))
            # Normalize gzip headers but retain byte-for-byte tar-split records.
            add_bytes(archive, target_db + "/tar-split.json.gz", gzip.compress(split, mtime=0))
            if (native_db / "descriptor.json").exists():
                add_bytes(archive, target_db + "/descriptor.json", regular(native_db / "descriptor.json"))
            add_dir(archive, prefix, 0o710)
            add_bytes(archive, prefix + "/link", link_id(chain).encode())
            add_link(archive, "overlay2/l/" + link_id(chain), "../" + cache + "/diff")
            if value["parent"]:
                add_bytes(archive, prefix + "/lower", ":".join(
                    "l/" + link_id(c) for c in reversed(value["ancestors"])).encode())
                add_dir(archive, prefix + "/work", 0o700)
                add_dir(archive, prefix + "/merged", 0o700)
            # Overlay2 uses 'committed' only as a hint; preserve it when present.
            if (source / "committed").exists():
                add_bytes(archive, prefix + "/committed", regular(source / "committed"))
            add_native_tree(archive, source / "diff", prefix + "/diff", observed=observed,
                            parent_view=parent_view, diff_root=source / "diff", whiteouts=whiteouts)
            for name in sorted(whiteouts):
                item = tarfile.TarInfo(prefix + "/diff/" + name)
                item.type = tarfile.CHRTYPE
                item.devmajor = item.devminor = item.mode = 0
                archive.addfile(item)
        observed["synthesized_whiteouts"] = len(whiteouts)
        return chain, {key: val for key, val in value.items() if key != "native_cache"} | {
            "fragment": "layers/" + path.name, "sha256": digest_file(path), "bytes": path.stat().st_size} | observed

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        packaged = dict(pool.map(package, sorted(layers.items())))
    manifest = {"schema": SCHEMA, "format": "docker-28-overlay2-native-fragments",
                "images": images, "references": selected, "layers": packaged,
                "compression": {"format": "gzip", "level": level}}
    (output / "manifest.json").write_bytes(canonical(manifest))
    return manifest


def merge(parts, output, pigz=None, level=6):
    output = Path(output)
    require(not output.exists(), "output archive already exists: " + str(output))
    images, layers, refs = {}, {}, {}
    for part in map(Path, parts):
        # Bazel materializes declared TreeArtifact inputs as symlinks inside its
        # sandbox. Follow those input links here; regular() deliberately keeps
        # stricter lstat validation for the mutable native store collector.
        manifest_path = part / "manifest.json"
        require(manifest_path.is_file(), "missing regular part manifest")
        value = json.loads(manifest_path.read_bytes())
        require(value.get("schema") == SCHEMA and value.get("format") == "docker-28-overlay2-native-fragments",
                "unsupported part manifest")
        for image_id, image in value["images"].items():
            require(DIGEST.fullmatch(image_id), "invalid part image ID")
            data = base64.b64decode(image["config"], validate=True)
            require(hashlib.sha256(data).hexdigest() == image_id[7:], "part config digest mismatch")
            config = json.loads(data)
            validate_image(config)
            require(config.get("rootfs", {}).get("type") == "layers", "unsupported part image rootfs")
            require(chain_ids(config["rootfs"]["diff_ids"]) == image["chains"], "part image chain mismatch")
            require(image_id not in images or images[image_id] == image, "conflicting image config")
            images[image_id] = image
        for ref, info in value["references"].items():
            require(ref not in refs or refs[ref] == info, "conflicting reference: " + ref)
            require(info["image_id"] in value["images"], "reference points outside its part")
            refs[ref] = info
        for chain, layer in value["layers"].items():
            require(DIGEST.fullmatch(chain), "invalid part ChainID")
            require(isinstance(layer.get("native_xattrs"), dict) and isinstance(layer.get("archive_xattrs"), dict),
                    "missing native xattr audit: " + chain)
            require(not layer["archive_xattrs"],
                    "unsupported native xattrs require an xattr-aware SONiC installer: " +
                    chain + " " + ",".join(sorted(layer["archive_xattrs"])))
            name = "layers/" + chain[7:] + ".tar.gz"
            require(layer["fragment"] == name, "unexpected fragment path")
            path = part / name
            require(path.is_file(), "missing regular fragment")
            require(digest_file(path) == layer["sha256"], "fragment digest mismatch: " + name)
            semantic = {key: layer[key] for key in ("diff", "parent", "size", "ancestors")}
            require(isinstance(semantic["size"], int) and semantic["size"] >= 0, "invalid part layer size")
            require(semantic["parent"] == (semantic["ancestors"][-1] if semantic["ancestors"] else ""),
                    "part layer parent/ancestor mismatch")
            if chain in layers:
                require(layers[chain][1] == semantic, "conflicting layer graph: " + chain)
            else:
                layers[chain] = (path, semantic)
    require(images and refs, "no images to merge")
    for image in images.values():
        require(set(image["chains"]) <= set(layers), "image has missing layer fragments")
    for chain, (_, value) in layers.items():
        require(set(value["ancestors"]) <= set(layers), "missing ancestor layer fragments")
        require(chain_ids([layers[c][1]["diff"] for c in value["ancestors"]] + [value["diff"]])[-1] == chain,
                "fragment chain identity mismatch")
    links = [link_id(chain) for chain in layers]
    require(len(links) == len(set(links)), "short overlay link collision")
    metadata = output.with_name(output.name + ".metadata.tmp.gz")
    try:
        with fragment(metadata, pigz, level) as archive:
            for name, mode in (("image", 0o700), ("image/overlay2", 0o700),
                               ("image/overlay2/imagedb", 0o700), ("image/overlay2/imagedb/content", 0o700),
                               ("image/overlay2/imagedb/content/sha256", 0o700),
                               ("image/overlay2/imagedb/metadata", 0o700),
                               ("image/overlay2/imagedb/metadata/sha256", 0o700),
                               ("image/overlay2/layerdb", 0o700), ("image/overlay2/layerdb/sha256", 0o700),
                               ("overlay2", 0o710), ("overlay2/l", 0o700)):
                add_dir(archive, name, mode)
            for image_id, image in sorted(images.items()):
                add_bytes(archive, "image/overlay2/imagedb/content/sha256/" + image_id[7:],
                          base64.b64decode(image["config"]))
            repositories = {}
            for ref, info in sorted(refs.items()):
                repositories.setdefault(info["repository"], {})[ref] = info["image_id"]
            add_bytes(archive, "image/overlay2/repositories.json", canonical({"Repositories": repositories}), 0o600)
        with output.open("wb") as combined:
            with metadata.open("rb") as data:
                shutil.copyfileobj(data, combined, 1024 * 1024)
            for chain, (path, _) in sorted(layers.items()):
                with path.open("rb") as data:
                    shutil.copyfileobj(data, combined, 1024 * 1024)
            combined.write(gzip.compress(b"\0" * 1024, mtime=0))
    finally:
        metadata.unlink(missing_ok=True)
    return {"schema": SCHEMA, "images": len(images), "layers": len(layers),
            "references": sorted(refs), "bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gather = commands.add_parser("collect")
    gather.add_argument("--store-root", required=True)
    gather.add_argument("--output", required=True)
    gather.add_argument("--tag", action="append", default=[])
    gather.add_argument("--jobs", type=int, default=1)
    combine = commands.add_parser("merge")
    combine.add_argument("--part", action="append", required=True)
    combine.add_argument("--output", required=True)
    for command in (gather, combine):
        command.add_argument("--pigz")
        command.add_argument("--level", type=int, choices=range(1, 10), default=6)
    args = parser.parse_args()
    if args.command == "collect":
        require(args.jobs > 0, "jobs must be positive")
        result = collect(args.store_root, args.output, args.tag, args.pigz, args.jobs, args.level)
        print(json.dumps({"images": len(result["images"]), "layers": len(result["layers"])}))
    else:
        print(json.dumps(merge(args.part, args.output, args.pigz, args.level)))


if __name__ == "__main__":
    main()
