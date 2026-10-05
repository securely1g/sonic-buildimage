# VS image execution worker

This AMD64 Debian Trixie image contains the execution tools for the existing
cacheable VS image graph: Bazel 8.5.1, Docker 28.5.2 with its native overlay2
store, Buildx 0.29.1, containerd 1.7.28, Python 3.13, j2cli, GNU tar, pigz,
SquashFS tools and the source-build tools used by SWSS. It does not contain a SONiC host filesystem,
service archives, source checkout or credentials. By default it uses Debian's
public trust anchors; an explicit local CA bundle can extend execution trust.

Build it from this directory's pinned public inputs:

```sh
bash tools/bazel/image/worker/prepare-worker-inputs.sh
docker build -t sonic-bazel-vs-worker:20261001 tools/bazel/image/worker
docker image inspect sonic-bazel-vs-worker:20261001
```

The input fetcher records public source URLs and verifies SHA256 hashes for
Docker, Buildx, containerd and the official Bazel binary. The Dockerfile pins the
Debian base by digest and validates these input hashes again. Its restricted Docker
context admits only those five public files, the Dockerfile, the public
`install-trust.py` helper and `build-ca-bundle.pem`. The preparation script copies
the helper from `tools/bazel/ci/trust.py` and creates an empty CA placeholder for
manual builds; the CI controller stages both files automatically. It installs
Debian execution packages from the public Trixie repositories and records every
installed package version in `/usr/share/sonic-image-worker-packages.txt`.
Those repositories can receive updates, so rebuilding the recipe may produce a
different image. CI builds the worker from this checked-out recipe, records its
actual immutable Docker image ID and checks its Bazel version. That ID goes into
the invocation's `execution-environment.json` and selects every worker container.
There is no saved worker or SONiC input release to publish or download.

For a managed TLS environment, the CI controller accepts an explicit
`--ca-bundle` PEM file. It validates certificate-only content, records the exact
input SHA256 and unique certificate count, and builds a distinct local worker
image. The helper installs system CA certificates and creates a Java trust
store from the pinned Bazel binary's embedded JDK, selected through
`/etc/bazel.bazelrc`. TLS verification stays enabled. With no additional CA,
the placeholder is empty and neither trust store is changed.

The worker's `/usr/local/share/sonic-build-trust/receipt.json` records only the
input hash, count, enabled flag and installed bundle hash. The installed bundle
retains Debian's public roots alongside the explicit anchors. Certificate bytes
stay in the local worker and invocation context; do not publish that worker or
context. They are not SONiC runtime inputs or CI evidence artifacts. The native
slave receives this explicit trust only through its build-only integration.

Run the build through `../run.py`. It starts a dedicated privileged worker with
an explicit build-directory mount and no host Docker socket; the image actions
also create private mount, PID and network namespaces. Docker's scratch data
must reside on the bind-mounted build filesystem, where overlay2 is supported.
The worker needs writable build/cache directories for UID/GID 1000. The native
preparation helper uses a separate instance with a private Docker daemon to run
the existing Make source build. It delegates controllers only inside its private
cgroup namespace, keeping worker processes in a separate leaf so nested memory
limits work on cgroup v2. Before Make, a nonroot preflight builds and runs a tiny
image made from the worker's BusyBox and libraries. This verifies BuildKit, the
private Docker socket, native memory/swap/file limits and writable bind mounts
without pulling another base image. Native outputs are verified and passed into
the Bazel graph in the same CI invocation, as described in the parent README.

The first worker built from this recipe occupied about 1.54 GB. It completed
real host finalization from the prepared VS snapshot and imported/collected a
native Docker service image. These checks validate the execution environment;
the full image job must still build and inspect its own final `sonic-vs.bin`.
