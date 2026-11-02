# VS installer assembly worker

This local AMD64 Debian Trixie worker contains Bazel 8.5.1, Docker 28.5.2 with
classic `overlay2`, containerd 1.7.28, Skopeo 1.18.0, Umoci 0.4.7, Buildx,
Python, j2cli, tar, pigz, zstd and SquashFS tools. It
contains execution tools only. Native host snapshots and service archives are
declared separately by [installer preparation](../README.md).

Build it from the repository root:

```sh
mkdir -p target
bash tools/bazel/image/worker/prepare-worker-inputs.sh
docker build --platform linux/amd64 \
  -t sonic-vs-assembly-worker:local tools/bazel/image/worker
```

The fetcher checks the SHA256 of the public Docker packages and Bazel binary;
the Dockerfile checks them again and pins the Debian base image by digest. The
restricted build context admits only the Dockerfile and these public tool
inputs. The worker uses Debian's public CA certificates. There is no custom
trust-bundle or general native-build controller integration.

Debian execution packages come from the public Trixie repositories, which can
change. Installed versions are recorded in
`/usr/share/sonic-image-worker-packages.txt`. Record the actual local worker image
ID rather than assuming that rebuilding the recipe preserves its identity:

```sh
python3 - <<'PY'
import json
from pathlib import Path
import subprocess

worker = json.loads(subprocess.check_output([
    "docker", "image", "inspect", "sonic-vs-assembly-worker:local",
]))[0]
if (worker["Os"], worker["Architecture"]) != ("linux", "amd64"):
    raise SystemExit("worker must be linux/amd64")
spec = {
    "schema": 1, "platform": "linux/amd64", "distribution": "trixie",
    "docker_version": "28.5.2", "storage_driver": "overlay2",
    "worker_image": worker["Id"],
}
Path("target/bazel-image-worker.json").write_text(json.dumps(spec, indent=2) + "\n")
PY
```

`../run.py` starts this exact local image with a declared build-directory mount,
no host Docker socket, and an optional shared repository-download cache. The
build and cache directories must be writable by worker UID/GID 1000. Docker
scratch storage belongs on the mounted build filesystem with working overlay2
support. Host finalization and image imports create private mount, PID, and
network namespaces inside the privileged worker.

The launcher supports a disposable worker or a named persistent worker retaining
its Bazel server. Lifecycle operations verify worker identity and serialize
access to the output root. See the [launcher example](../README.md#assembly-and-cache-boundaries)
for building and stopping a persistent worker. This worker assembles already
prepared inputs; the normal outer Make build produces native predecessors.

For the optional containerd backend, Skopeo converts the declared service
archives and containerd imports them without unpacking. Umoci extracts only
local-package files needed for host plugins. The same worker supports both
backends; its image ID remains an explicit action input.
