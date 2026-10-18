# Source-built Debian package

`//src/sonic-supervisord-utilities-rs/debian:metadata` runs the declared
`dpkg-shlibdeps` and `dpkg-gencontrol` tools against the existing release
runtime TAR plus the original compressed Debian changelog, and this directory's
original control/changelog. The libc and GCC
runtime inputs include their original Debian symbols files. Common supplies
its source-built library and newly generated symbols metadata.

For example, the checked AMD64 listener needs `libc6 (>= 2.34)`,
`libgcc-s1 (>= 4.2)` and `libswsscommon (>= 1.0.0)`. These values are outputs of
the ELF analysis; the build definition does not hardcode them.

The separate `:package` target passes that final control and `:package_data`
to `sonic_deb`. The package data combines the unchanged executable with
`/usr/share/doc/sonic-supervisord-utilities-rs/changelog.gz`, which the original
Make debhelper flow also installs. It is tagged `manual` so wildcard builds do not
produce a DEB. This source component has no maintainer scripts or conffiles.
Its existing runtime and debug TARs remain available.

## Draft dependencies and validation

The package assembler comes from [sonic-build-infra #45](https://github.com/securely1g/sonic-build-infra/pull/45), and the declared Debian metadata tools come from
[#46](https://github.com/securely1g/sonic-build-infra/pull/46). Common's source
library and symbols remain the metadata inputs; [Common #26](https://github.com/securely1g/sonic-swss-common/pull/26)
provides the real dependency DEB for installed-package validation.

The native workflow selects a reviewed infrastructure snapshot from
[registry #73](https://github.com/securely1g/sonic-bazel-registry/pull/73) and
records the temporary registry revision. The checked-in registry URLs continue
to use `main`. These prerequisites remain Draft until their source and registry
changes land.

[Native AMD64 and ARM64 validation passed](https://github.com/securely1g/sonic-buildimage/actions/runs/39095381937):

- Original Rust tests, release runtime checks and detached debug-symbol checks.
- Actual `:package` creation and archive validation against its payload/control.
- Fresh Debian installation with real source-built Common and libyang packages.
- Listener startup, same-version reinstall, removal, purge and clean dpkg state.

The package keeps its original `1.0.0` version, so these checks do not claim a
transition between different listener versions. Final container service and
watchdog behavior are validated by the image consumer.
