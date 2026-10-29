"""Validate the kernel producer's schema-1 boot artifact pairing."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


def boot_inputs(metadata_path, boot_tree):
    metadata = json.loads(Path(metadata_path).read_text())
    if metadata.get("schema") != 1:
        raise ValueError("unsupported kernel metadata schema")
    release = metadata["kernel_release"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", release):
        raise ValueError("invalid kernel release")
    for kind in ("vmlinuz", "config", "System.map"):
        record = metadata["boot"][kind]
        relative = PurePosixPath(record["path"])
        if relative != PurePosixPath("boot", kind + "-" + release):
            raise ValueError("kernel boot path/release mismatch: " + str(relative))
        source = Path(boot_tree) / str(relative)
        # Bazel may materialize declared tree inputs as symlinks inside the
        # action sandbox. The source producer checks regular output files;
        # this consumer verifies the declared bytes against that manifest.
        if not source.is_file():
            raise ValueError("missing kernel boot artifact: " + str(source))
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise ValueError("kernel boot SHA256 mismatch: " + str(relative))
    # Preserve the complete prepared boot directory, including signed EFI and DTB
    # resources. The three manifest records above remain the kernel identity check.
    return metadata, sorted((Path(boot_tree) / "boot").rglob("*"))


def boot_mode(spec, relative, directory=False):
    """Explicit logical permissions, independent of Bazel artifact host modes."""
    default = "0744" if directory else ("0600" if Path(relative).name.startswith("System.map-") else "0644")
    return int(spec.get("boot_file_modes", {}).get(relative, default), 8)
