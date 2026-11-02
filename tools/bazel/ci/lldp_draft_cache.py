#!/usr/bin/env python3
"""Preserve setup-bazel caches when the temporary LLDP wrapper skips home RC."""

from pathlib import Path

home = Path.home() / ".bazelrc"
settings = [line for line in home.read_text().splitlines()
            if line.startswith(("build --disk_cache=", "common --repository_cache="))]
if len(settings) != 2 or len({line.split("=", 1)[0] for line in settings}) != 2:
    raise SystemExit("setup-bazel disk/repository cache settings are missing")
user = Path(".bazelrc.user")
current = user.read_text() if user.exists() else ""
user.write_text(current + "\n" + "\n".join(settings) + "\n")
