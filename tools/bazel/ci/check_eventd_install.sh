#!/bin/bash
# Exercise only real source packages in a disposable native Debian container.
set -euo pipefail
if [ ! -f /.dockerenv ] || [ "${SONIC_DEB_TEST_CONTAINER:-}" != 1 ]; then
    echo 'Run only in the disposable Debian package-test container.' >&2
    exit 1
fi
package_directory=$(realpath "$1")
architecture=$(dpkg --print-architecture)
packages=(sonic-eventd sonic-rsyslog-plugin)
files=()
for package in "${packages[@]}"; do
    archive="$package_directory/${package}_1.0.0-0_${architecture}.deb"
    test -f "$archive"
    files+=("$archive")
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y --no-install-recommends install python3 "$package_directory"/dependencies/*.deb "${files[@]}"
for package in "${packages[@]}"; do
    test "$(dpkg-query -W -f='${Status}' "$package")" = 'install ok installed'
    test "$(dpkg-query -W -f='${Version}' "$package")" = 1.0.0-0
done
apt-get check
test -z "$(dpkg --audit)"
for binary in eventd eventdb events_tool rsyslog_plugin; do
    ldd "/usr/bin/$binary" > "$package_directory/$binary.ldd.txt"
    if grep -F 'not found' "$package_directory/$binary.ldd.txt"; then exit 1; fi
    dpkg-query -S "/usr/bin/$binary"
done
# This original CLI returns 1 when it prints help; reject loader failures.
result=0
rsyslog_plugin -h > "$package_directory/plugin-help.txt" 2>&1 || result=$?
test "$result" = 1
grep -F 'Usage for rsyslog_plugin' "$package_directory/plugin-help.txt"
python3 -c 'import json; json.load(open("/etc/eventd.json")); json.load(open("/etc/evprofile/default.json"))'
configuration=/etc/eventd.json
dpkg-query -W -f='${Conffiles}' sonic-eventd | grep -F "$configuration"
dpkg-query -W -f='${Conffiles}' sonic-rsyslog-plugin | grep -F /etc/monit/conf.d/monit_events
printf '\n' >> "$configuration"
before=$(sha256sum "$configuration")
apt-get -y --reinstall install "${files[@]}"
test "$(sha256sum "$configuration")" = "$before"
apt-get -y remove "${packages[@]}"
test ! -e /usr/bin/eventd
test ! -e /usr/bin/rsyslog_plugin
test -f "$configuration"
test -f /etc/monit/conf.d/monit_events
apt-get -y purge "${packages[@]}"
test ! -e "$configuration"
test ! -e /etc/evprofile/default.json
test ! -e /etc/monit/conf.d/monit_events
apt-get check
test -z "$(dpkg --audit)"
printf '%s\n' '{"initial_configuration":"passed","elf_dependencies":"passed","plugin_help":"passed","reinstall":"passed","conffile_preservation":"passed","remove":"passed","purge":"passed","version_upgrade":"not tested: the existing source package version is 1.0.0-0"}' > "$package_directory/installation.json"
