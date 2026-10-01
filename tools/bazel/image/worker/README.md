# VS image execution worker

This AMD64 Debian Trixie image contains the execution tools for the existing
cacheable VS image graph: Bazel 8.5.1, Docker 28.5.2 with its native overlay2
store, containerd 1.7.28, Python 3.13, j2cli, GNU tar, pigz, SquashFS tools and
the source-build tools used by SWSS. It does not contain a SONiC host filesystem,
service archives, source checkout, credentials or private trust anchors.

Build it from this directory's pinned public inputs:

```sh
bash tools/bazel/image/worker/prepare-worker-inputs.sh
docker build -t sonic-bazel-vs-worker:20261001 tools/bazel/image/worker
docker image inspect sonic-bazel-vs-worker:20261001
docker save sonic-bazel-vs-worker:20261001 | gzip -n > sonic-bazel-vs-worker-20261001.tar.gz
sha256sum sonic-bazel-vs-worker-20261001.tar.gz
```

The input fetcher records public source URLs and verifies SHA256 hashes for
Docker, containerd and the official Bazel binary. The Dockerfile pins the Debian
base by digest and validates these input hashes again. Its restricted Docker
context admits only those four public files and the Dockerfile. It installs
Debian execution packages from the public Trixie repositories and records every
installed package version in `/usr/share/sonic-image-worker-packages.txt`.
Those repositories can receive updates, so rebuilding the recipe may produce a
different image. CI consumes an immutable saved-image archive with a verified
SHA256, rather than rebuilding this worker during each image job.

After verifying the archive checksum, load it with `docker load --input` and
inspect its declared tag. A Docker engine using the containerd image store can
report a manifest/index digest as the local image ID; the classic image store
reports the image-config digest. The release descriptor records both permitted
identities. Reject any other identity and use the verified destination-local ID
when creating the separate `execution-environment.json` for that CI run. Keep
the Docker version, platform and overlay2 requirements from the descriptor.

Run the build through `../run.py`. It starts a dedicated privileged worker with
an explicit build-directory mount and no host Docker socket; the image actions
also create private mount, PID and network namespaces. Docker's scratch data
must reside on the bind-mounted build filesystem, where overlay2 is supported.
The worker needs writable build/cache directories for UID/GID 1000. Native
predecessor inputs are supplied separately as described in the parent README.

The first worker built from this recipe occupied about 1.54 GB. It completed
real host finalization from the prepared VS snapshot and imported/collected a
native Docker service image. These checks validate the execution environment;
the full image job must still build and inspect its own final `sonic-vs.bin`.
