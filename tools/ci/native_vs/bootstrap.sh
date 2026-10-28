#!/usr/bin/env bash
# Provision the disposable Debian execution environment for direct Make/Bazel CI.
# This script never builds, pulls, or starts a sonic-slave image.
set -euo pipefail

if [[ $(id -u) != 0 || ! -f /.dockerenv ]]; then
    echo 'Run bootstrap.sh as root inside the disposable Debian CI container.' >&2
    exit 1
fi
# shellcheck source=/dev/null
. /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_CODENAME:-} != trixie || $(uname -m) != x86_64 ]]; then
    echo 'The direct VS build requires native AMD64 Debian Trixie.' >&2
    exit 1
fi
: "${VS_UID:?Set VS_UID to the host checkout owner UID}"
: "${VS_GID:?Set VS_GID to the host checkout owner GID}"
[[ $VS_UID =~ ^[1-9][0-9]*$ && $VS_GID =~ ^[1-9][0-9]*$ ]]
workspace=$(pwd -P)
setup_dir="$workspace/tools/ci/native_vs"
artifacts="$workspace/artifacts/vs/native-bootstrap"
mkdir -p "$artifacts"
export DEBIAN_FRONTEND=noninteractive
# Provisioning runs as root even though later docker-exec calls use vsbuild.
export HOME=/root
umask 022

# Installing Docker/QEMU dependencies must not start another daemon or services
# in the execution container. Image construction still uses the host socket.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d

# Include source indexes for apt-get build-dep. Preserve the pinned base's
# existing Debian suite/mirror selection instead of mixing Ubuntu packages.
sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
apt-get update
mapfile -t packages < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$setup_dir/apt-packages.txt")
apt-get install -y --no-install-recommends "${packages[@]}"
# Both KVM installer console scripts spawn telnet through pexpect.
command -v telnet
mapfile -t source_packages < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$setup_dir/debian-sources.txt")
apt-get build-dep -y --no-install-recommends "${source_packages[@]}"
# Existing legacy Make recipes install Python wheels into this disposable root.
install -m 0644 sonic-slave-trixie/pip.conf /etc/pip.conf
python3 -m pip install -r "$setup_dir/requirements.txt"

# Preserve account ownership across the bind mount and let Make perform its
# existing package-install/chroot steps. Do not run source builds as root.
if getent passwd "$VS_UID" >/dev/null; then
    echo "The fresh execution container already has UID $VS_UID" >&2
    exit 1
fi
if ! getent group "$VS_GID" >/dev/null; then
    groupadd --gid "$VS_GID" vsbuild
fi
useradd --uid "$VS_UID" --gid "$VS_GID" --create-home --home-dir /home/vsbuild --shell /bin/bash vsbuild
if [[ -S /var/run/docker.sock ]]; then
    socket_gid=$(stat -c %g /var/run/docker.sock)
    if ! getent group "$socket_gid" >/dev/null; then
        groupadd --gid "$socket_gid" native-docker
    fi
    usermod --append --groups "$(getent group "$socket_gid" | cut -d: -f1)" vsbuild
fi
if [[ -e /dev/kvm ]]; then
    kvm_gid=$(stat -c %g /dev/kvm)
    if ! getent group "$kvm_gid" >/dev/null; then
        groupadd --gid "$kvm_gid" native-kvm
    fi
    usermod --append --groups "$(getent group "$kvm_gid" | cut -d: -f1)" vsbuild
fi
printf 'vsbuild ALL=(ALL) NOPASSWD: ALL\nDefaults:vsbuild !requiretty\n' > /etc/sudoers.d/vsbuild
chmod 0440 /etc/sudoers.d/vsbuild
visudo -cf /etc/sudoers.d/vsbuild
sudo -u vsbuild -H git config --global user.name 'SONiC CI'
sudo -u vsbuild -H git config --global user.email securely1g@gmail.com
# Retained P4RT recipes name /sonic explicitly; Docker bind paths still use the
# real, identical host/container workspace path configured by the workflow.
if [[ $workspace != /sonic ]]; then
    test ! -e /sonic
    ln -s "$workspace" /sonic
fi
lockdir=${DOCKER_LOCKDIR:-/tmp/docklock}
lockfile=${DOCKER_LOCKFILE_SAVE:-$lockdir/docker_save.lock}
install -d -o "$VS_UID" -g "$VS_GID" "$workspace/target"
mkdir -p "$lockdir"
chown "$VS_UID:$VS_GID" "$lockdir"
install -o "$VS_UID" -g "$VS_GID" -m 0644 /dev/null "$lockfile"
git config --global --add safe.directory "$workspace"

# Reuse the existing package recipe and version generator for build hooks.
# These files are also inputs to the service-image Docker builds.
make --no-print-directory -f - <<'MAKE'
include rules/config
-include rules/config.organization
-include rules/config.user
export BUILD_PACKAGES_URL SONIC_VERSION_CONTROL_COMPONENTS SONIC_VERSION_CACHE_SOURCE
export MIRROR_SNAPSHOT BUILD_PUBLIC_URL BUILD_SNAPSHOT_URL PIP_HTTP_TIMEOUT
export TRUSTED_GPG_URLS
.PHONY: prepare-native-hooks
prepare-native-hooks:
	DISTRO=trixie SONIC_VERSION_CACHE= scripts/generate_buildinfo_config.sh
	$(MAKE) -C src/sonic-build-hooks all
	MIRROR_URLS=https://deb.debian.org/debian/ MIRROR_SECURITY_URLS=https://security.debian.org/debian-security/ scripts/build_mirror_config.sh sonic-slave-trixie amd64 trixie
MAKE
# The hook refresh reads /etc/apt/sources.list; retain the same generated mirror
# configuration used by native service-image builds.
install -m 0644 sonic-slave-trixie/sources.list.amd64 /etc/apt/sources.list
rm /etc/apt/sources.list.d/debian.sources
mkdir -p sonic-slave-trixie/buildinfo
cp -a src/sonic-build-hooks/buildinfo/. sonic-slave-trixie/buildinfo/
dpkg -i src/sonic-build-hooks/buildinfo/sonic-build-hooks_1.0_all.deb
python3 scripts/versions_manager.py generate \
    -t sonic-slave-trixie/buildinfo/versions -n build-sonic-slave-trixie \
    -d trixie -a amd64
cp -a sonic-slave-trixie/buildinfo/. /usr/local/share/buildinfo/
pre_run_buildinfo build-sonic-slave-trixie
chmod -R a+rwX /usr/local/share/buildinfo

# Native Go recipes retain the selected FIPS compiler. Read the authoritative
# version and URLs from Make rather than maintaining another version catalog.
mapfile -t go_config < <(make --no-print-directory -f - <<'MAKE'
BLDENV := trixie
CONFIGURED_ARCH := amd64
include rules/config
-include rules/config.organization
-include rules/config.user
include rules/functions
include rules/sonic-fips.mk
.PHONY: print-go
print-go:
	@printf '%s\n' '$(FIPS_GOLANG_MAIN_VERSION)' '$(FIPS_GOLANG_VERSION)' '$(BUILD_PUBLIC_URL)' '$(FIPS_VERSION)'
MAKE
)
[[ ${#go_config[@]} == 4 ]]
go_major=${go_config[0]}
go_version=${go_config[1]}
go_packages=()
for name in "golang-$go_major-go_${go_version}_amd64.deb" "golang-$go_major-src_${go_version}_all.deb"; do
    curl --fail --location --retry 3 --connect-timeout 20 --max-time 600 --output "/tmp/$name" \
        "${go_config[2]}/fips/trixie/${go_config[3]}/amd64/$name"
    go_packages+=("/tmp/$name")
done
dpkg -i "${go_packages[@]}"
rm "${go_packages[@]}"
ln -s "/usr/lib/go-$go_major" /usr/local/go

# Match the maintained native Rust tools; Bazel's toolchain remains independent.
# Read only the existing explicit version declarations, never execute Dockerfile
# instructions or copy its broad hardware-vendor setup into this job.
python3 - "$artifacts/native-tool-versions.json" <<'PY'
import json, re, sys
from pathlib import Path
text = Path('sonic-slave-trixie/Dockerfile.j2').read_text()
versions = {}
for name, pattern in {
    'rust': r'--default-toolchain ([0-9]+\.[0-9]+\.[0-9]+) -y',
    'cargo-auditable': r'cargo install --locked cargo-auditable@([0-9]+\.[0-9]+\.[0-9]+)',
    'rust-audit-info': r'cargo install --locked rust-audit-info@([0-9]+\.[0-9]+\.[0-9]+)',
    'cargo-tarpaulin': r'cargo install --locked cargo-tarpaulin@([0-9]+\.[0-9]+\.[0-9]+)',
}.items():
    matches = re.findall(pattern, text)
    if len(set(matches)) != 1:
        raise SystemExit(f'Expected one maintained {name} version, got {matches}')
    versions[name] = matches[0]
Path(sys.argv[1]).write_text(json.dumps(versions, indent=2) + '\n')
PY
export CARGO_HOME=/usr/.cargo RUSTUP_HOME=/usr/.cargo
curl --fail --location --retry 3 --connect-timeout 20 --max-time 120 \
    --output /tmp/rustup-init.sh https://sh.rustup.rs
bash /tmp/rustup-init.sh -y --no-modify-path --profile minimal \
    --default-toolchain "$(jq -r .rust "$artifacts/native-tool-versions.json")"
rm /tmp/rustup-init.sh
export PATH="/usr/local/go/bin:$CARGO_HOME/bin:$PATH"
for tool in cargo-auditable rust-audit-info cargo-tarpaulin; do
    cargo install --locked "$tool@$(jq -r --arg name "$tool" '.[$name]' "$artifacts/native-tool-versions.json")"
done
install -m 0755 files/build/cargo-wrapper /usr/local/bin/cargo
chmod -R a+rX /usr/.cargo
cat > /etc/profile.d/sonic-native-tools.sh <<'PROFILE'
export PATH=/usr/local/go/bin:/usr/.cargo/bin:$PATH
export CARGO_HOME=/usr/.cargo
export RUSTUP_HOME=/usr/.cargo
PROFILE
# Source builds need writable Cargo registry state, as in the current builder.
chown -R "$VS_UID:$VS_GID" /usr/.cargo

# Keep actual installed versions and bootstrap inputs as reproducibility evidence.
dpkg-query -W -f='${binary:Package}\t${Version}\n' > "$artifacts/debian-packages.tsv"
python3 -m pip freeze --all > "$artifacts/python-packages.txt"
sha256sum tools/ci/native_vs/apt-packages.txt tools/ci/native_vs/debian-sources.txt \
    tools/ci/native_vs/requirements.txt tools/ci/native_vs/bootstrap.sh \
    > "$artifacts/bootstrap-inputs.sha256"
/usr/local/go/bin/go version > "$artifacts/go-version.txt"
/usr/.cargo/bin/rustc --version > "$artifacts/rust-version.txt"
chown -R "$VS_UID:$VS_GID" "$artifacts" "$workspace/target/vcache" \
    sonic-slave-trixie/buildinfo src/sonic-build-hooks/buildinfo src/sonic-build-hooks/tmp
for directory in "$workspace/target" "$workspace/target/vcache" "$artifacts" /usr/.cargo; do
    sudo -u vsbuild -H test -w "$directory"
done
apt-get clean
printf 'Native Debian bootstrap ready for UID %s:GID %s\n' "$VS_UID" "$VS_GID"
