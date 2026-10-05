#!/usr/bin/env python3
"""Create a tiny image layer and vary only archive source metadata."""

import argparse
import io
import os
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer", type=Path)
    parser.add_argument("--copy", type=Path)
    parser.add_argument("--first", type=Path)
    parser.add_argument("--second", type=Path)
    args = parser.parse_args()
    if args.layer:
        payload = b"SONiC archive reproducibility fixture\n"
        with tarfile.open(args.layer, "w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo("etc/archive-fixture")
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    else:
        if not (args.copy and args.first and args.second):
            parser.error("--copy requires --first and --second")
        payload = args.copy.read_bytes()
        for path, timestamp in ((args.first, 946684800), (args.second, 1700000000)):
            path.write_bytes(payload)
            os.utime(path, (timestamp, timestamp))


if __name__ == "__main__":
    main()
