#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
# The CI controller stages these explicitly. Keep direct recipe builds working
# with the public helper and no additional CA trust by default.
if [[ -f ../../ci/trust.py ]]; then
  cp ../../ci/trust.py install-trust.py
elif [[ ! -f install-trust.py ]]; then
  echo 'The isolated worker context is missing install-trust.py' >&2
  exit 1
fi
if [[ ! -e build-ca-bundle.pem ]]; then
  : > build-ca-bundle.pem
fi
if [[ -s build-ca-bundle.pem ]]; then
  python3 install-trust.py --validate --bundle build-ca-bundle.pem
fi
curl --fail --location --retry 3 \
  https://releases.bazel.build/8.5.1/release/bazel-8.5.1-linux-x86_64 -o bazel
curl --fail --location --retry 3 \
  'https://download.docker.com/linux/debian/dists/trixie/pool/stable/amd64/docker-ce_28.5.2-1~debian.13~trixie_amd64.deb' -o docker-ce.deb
curl --fail --location --retry 3 \
  'https://download.docker.com/linux/debian/dists/trixie/pool/stable/amd64/docker-ce-cli_28.5.2-1~debian.13~trixie_amd64.deb' -o docker-ce-cli.deb
curl --fail --location --retry 3 \
  'https://download.docker.com/linux/debian/dists/trixie/pool/stable/amd64/containerd.io_1.7.28-2~debian.13~trixie_amd64.deb' -o containerd.io.deb
sha256sum --check <<'CHECKSUMS'
61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e  bazel
10f6fba7cfe2309cfb3a14033218339c6f0687247903806e6298943fca0ca740  docker-ce.deb
dde4a0613e538847ed8558dbd937accd31fb76fca9c115e42442b812b7d13c0a  docker-ce-cli.deb
18fad97fa08cb1e5f1f76f3dfd9a571e83f11e135f13ef77b83f2aa35397a619  containerd.io.deb
CHECKSUMS
