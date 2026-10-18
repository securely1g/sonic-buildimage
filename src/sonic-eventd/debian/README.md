
Native CI also builds Common's actual library DEB and its source-built libyang
runtime dependency, then installs the event packages in a fresh Debian container.
It checks dpkg configuration, ELF library loading, plugin CLI startup, configuration
preservation on reinstall/removal, and cleanup on purge. The Debian payload includes
parent directory entries: unlike ordinary TAR extraction, dpkg cannot unpack
`/etc/evprofile/default.json` when its directory is absent from both the package
and the base filesystem. The fresh-container check detects that failure.

The source version stays `1.0.0-0`; the installation check covers same-version
replacement and does not claim an upgrade between different release versions.
