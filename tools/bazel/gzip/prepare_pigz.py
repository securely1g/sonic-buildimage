#!/usr/bin/env python3
"""Prepare the original BCR pigz archive when its upstream URL changes bytes."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.gzip.source_archive import publish


# Unchanged https://bcr.bazel.build/modules/pigz/2.8.bcr.2/source.json.
VERSION = "2.8.bcr.2"
SHA256 = "eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0"
URL = "https://distfiles.macports.org/pigz/pigz-2.8.tar.gz"


def prepare(module, directory, fetch=None):
    selections = [ast.literal_eval(kw.value)
                  for node in ast.walk(ast.parse(module.read_text()))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "bazel_dep"
                  and any(kw.arg == "name" and ast.literal_eval(kw.value) == "pigz"
                          for kw in node.keywords)
                  for kw in node.keywords if kw.arg == "version"]
    if selections != [VERSION]:
        raise ValueError("pigz selection changed; review its BCR source integrity")
    destination = directory / "pigz-2.8.tar.gz"
    cached = destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == SHA256
    if not cached:
        if fetch is None:
            with urlopen(URL, timeout=120) as response:
                data = response.read()
        else:
            data = fetch(URL)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError("pigz mirror bytes differ from the unchanged BCR SHA256")
        publish(data, destination)
    return {"module": "pigz", "version": VERSION, "url": URL,
            "archive": str(destination), "sha256": SHA256,
            "bytes": destination.stat().st_size, "reused_verified_archive": cached}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, default=Path("MODULE.bazel"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.module, args.output), indent=2))


if __name__ == "__main__":
    main()
