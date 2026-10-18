#!/bin/bash
# Exercise only real source packages in a disposable native Debian container.
set -euo pipefail
if [ ! -f /.dockerenv ] || [ "${SONIC_DEB_TEST_CONTAINER:-}" != 1 ]; then
    echo 'Run only in the disposable Debian package-test container.' >&2
    exit 1
fi
package_directory=$(realpath "$1")
architecture=$(dpkg --print-architecture)
package=sonic-supervisord-utilities-rs
archive="$package_directory/${package}_1.0.0_${architecture}.deb"
test -f "$archive"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y --no-install-recommends install "$package_directory"/dependencies/*.deb "$archive"
test "$(dpkg-query -W -f='${Status}' "$package")" = 'install ok installed'
test "$(dpkg-query -W -f='${Version}' "$package")" = 1.0.0
test "$(dpkg-query -W -f='${Status}' libswsscommon)" = 'install ok installed'
supervisor-proc-exit-listener-rs --help
dpkg-query -S /usr/bin/supervisor-proc-exit-listener-rs
apt-get check
test -z "$(dpkg --audit)"
apt-get -y --reinstall install "$archive"
supervisor-proc-exit-listener-rs --help
apt-get -y remove "$package"
test ! -e /usr/bin/supervisor-proc-exit-listener-rs
# With no conffiles, removal drops the local-only package from APT's state.
# Install the same real DEB again to test purge independently.
apt-get -y install "$archive"
apt-get -y purge "$package"
test ! -e /usr/bin/supervisor-proc-exit-listener-rs
apt-get check
test -z "$(dpkg --audit)"
printf '%s\n' '{"initial_configuration":"passed","runtime_help":"passed","reinstall":"passed","remove":"passed","purge":"passed","version_upgrade":"not tested: the existing source package version is 1.0.0"}' > "$package_directory/installation.json"
