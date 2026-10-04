FROM debian:trixie-20260918@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c

# These public inputs are fetched by prepare-worker-inputs.sh. The controller
# may additionally supply an explicit certificate-only CA bundle for local TLS
# environments; the default placeholder is empty. No host rootfs or credentials
# are part of the build context.
COPY docker-ce.deb docker-ce-cli.deb containerd.io.deb docker-buildx-plugin.deb /tmp/packages/
RUN printf '%s\n' \
    '10f6fba7cfe2309cfb3a14033218339c6f0687247903806e6298943fca0ca740  /tmp/packages/docker-ce.deb' \
    'dde4a0613e538847ed8558dbd937accd31fb76fca9c115e42442b812b7d13c0a  /tmp/packages/docker-ce-cli.deb' \
    '18fad97fa08cb1e5f1f76f3dfd9a571e83f11e135f13ef77b83f2aa35397a619  /tmp/packages/containerd.io.deb' \
    '355079ff06148070b255a0c0fd5a1672f2a960e9938605dea50a010345eb5d17  /tmp/packages/docker-buildx-plugin.deb' \
    | sha256sum -c - \
    && printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       autoconf automake binutils build-essential busybox ca-certificates \
       acl curl diffutils file findutils gawk git grep gzip iproute2 iptables \
       j2cli=0.3.12b-5 jq kmod libtool make mount passwd pigz pkg-config \
       procps psmisc python3 sed squashfs-tools=1:4.6.1-1 sudo tar unzip \
       util-linux wget xz-utils zip zstd /tmp/packages/*.deb \
    && dpkg-query -W -f='${binary:Package}=${Version}\n' > /usr/share/sonic-image-worker-packages.txt \
    && rm -rf /var/lib/apt/lists/* /tmp/packages

COPY bazel /usr/local/bin/bazel
RUN echo '61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e  /usr/local/bin/bazel' | sha256sum -c - \
    && chmod 755 /usr/local/bin/bazel \
    && test "$(docker --version)" = 'Docker version 28.5.2, build ecc6942' \
    && test "$(dockerd --version)" = 'Docker version 28.5.2, build 89c5e8f' \
    && test "$(docker buildx version | awk '{print $2}')" = 'v0.29.1' \
    && test "$(bazel --version)" = 'bazel 8.5.1'

# This changes the immutable worker identity when optional local trust is used.
# It affects only the execution worker; the CA file is never a SONiC image input.
COPY install-trust.py build-ca-bundle.pem /tmp/trust-inputs/
RUN python3 /tmp/trust-inputs/install-trust.py --bundle /tmp/trust-inputs/build-ca-bundle.pem \
      --bazel /usr/local/bin/bazel > /usr/share/sonic-build-trust.json \
    && rm -rf /tmp/trust-inputs

# Give Python requests/pip the same verified system trust used by git/curl.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

ENV LANG=C.UTF-8
CMD ["/bin/bash"]
