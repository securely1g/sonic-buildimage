# Build the listener Debian package

The package target uses the existing source-built release executable and the
original `control` and `changelog` in this directory:

```sh
bazel build //src/sonic-supervisord-utilities-rs/debian:package
```

It produces `sonic-supervisord-utilities-rs_1.0.0_<architecture>.deb` on native
AMD64 and ARM64. `dpkg-shlibdeps` derives dependencies from that executable and
its declared library metadata; `dpkg-gencontrol` fills the source control fields.
The payload also includes the original compressed changelog. The component has
no maintainer scripts or configuration files. Runtime and detached debug TARs
remain available through the parent BUILD file.

The native workflow builds the DEB, retains it as an Actions artifact, and checks
its control fields, full payload bytes, ownership, modes and ELF architecture.
It also runs the original source and runtime/debug tests. A separate installed
package check needs the source-built Common DEB; archive checks alone do not
establish successful installation of that dependency closure.

During review, the workflow checks out the exact shared packaging and metadata
changes from sonic-build-infra PRs #45 and #46 and library symbols from
sonic-swss-common PR #25. The image integration in buildimage PR #33 consumes
this package separately.
