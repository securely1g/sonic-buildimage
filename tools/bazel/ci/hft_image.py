"""Compare the HFT service files with the Make Dockerfile and its SWSS parent."""

import json
from pathlib import Path, PurePosixPath

from tools.bazel.ci.artifact_validation import require, sha, verify_inherited_files


SOURCE = Path(__file__).resolve().parents[3] / "dockers/docker-hft"


def make_files():
    expected = {}
    for line in (SOURCE / "Dockerfile.j2").read_text().splitlines():
        if line.startswith("COPY "):
            *sources, destination = json.loads(line.removeprefix("COPY "))
            require(destination.startswith("/") and destination.endswith("/"), "Unexpected HFT COPY destination")
            for source in sources:
                file = SOURCE / source
                path = str(PurePosixPath(destination.lstrip("/"), file.name))
                expected[path] = {"kind": "file", "sha256": sha(file), "size": file.stat().st_size, "uid": 0, "gid": 0,
                                  "mode": 0o755 if file.stat().st_mode & 0o100 else 0o644}
        elif line.startswith("RUN chmod "):
            mode, *paths = line.removeprefix("RUN chmod ").split()
            for path in paths:
                expected[path.lstrip("/")]["mode"] = int(mode, 8)
    return expected


def verify_payload(parent, files, owners, debug):
    expected = make_files()
    require(expected and expected == owners["service.files.tar"], "HFT configuration differs from the Make COPY files")
    if debug:
        expected = expected | owners["hft.debug-tools.tar"] | owners["hft.debug-accounts.tar"]
    else:
        for path, record in files.items():
            require(record["kind"] == "directory" or path in parent or path in expected,
                    "HFT runtime adds a file outside the Make Dockerfile: " + path)
    # The debug status is independently matched against the installed_packages output.
    verify_inherited_files(parent, files, expected, {"var/lib/dpkg/status"} if debug else set(), "HFT")
