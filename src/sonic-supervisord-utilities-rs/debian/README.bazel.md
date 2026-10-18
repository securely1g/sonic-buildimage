# Source-built Debian package prototype

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

## Draft dependencies and validation boundary

This prototype requires the metadata API in [sonic-build-infra #45](https://github.com/securely1g/sonic-build-infra/pull/45), the tools in
[#46](https://github.com/securely1g/sonic-build-infra/pull/46), and [Common #25](https://github.com/securely1g/sonic-swss-common/pull/25)'s source-owned `//dist:libswsscommon_symbols` target. The current
image registry pins do not contain all three. Use explicit local module
overrides to review the action graph until those dependencies are published;
do not replace the image's other infrastructure changes with an older release.

An action query can inspect `:package` without creating an archive. Building
`:metadata` creates text files and the existing source/runtime dependencies.
Executing `:package` is the distinct DEB-producing step and has not been run.

Native AMD64 and ARM64 CI checks out those exact reviewed commits, combines the
two infrastructure changes without committing, and records the resulting tree
hash. It audits and builds only `:metadata`, validates the resulting control,
and retains the proposed DEB assembly action from `aquery`. Package creation and
installation validation remain pending.
