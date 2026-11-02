# AMD64 database source image

The supported profile is native AMD64 Debian Trixie VS, with FIPS enabled and
ASAN/cross/QEMU modes disabled. The runtime image inherits the source-built
config-engine parent, consumes DASH API and its Protobuf runtime directly from
their public source TAR targets, and adds locked Debian Redis and the locked
Click wheel. Common and sonic-db-cli are inherited once. No DEB is generated.

The debug image extends the exact runtime image. It carries matching source
symbols, locked Debian debugging tools and the source-built FIPS OpenSSH client.
Real imported Debian control records extend the inherited package state. The
selected DASH source owner supplies TARs without a package-identity interface;
this image does not invent dpkg records for them.

The original database scripts and five templates are installed unchanged.
Upstream expand_template applies the original Dockerfile's Redis settings to
the pinned package configuration; CI compares its bytes with that exact sed
recipe. Redis account identities are inherited. Directory/configuration modes
and the configuration statoverride follow the real Debian postinst scripts.
The existing supervisor entrypoint starts services; no image-build action starts
host services or executes online apt.

Make keeps image names, host redis-cli, mounts, prerequisites and selection flags.
The supported producer uses the common Bazel archive bridge. Its event-database
selection is an atomic declared environment file: unchanged bytes retain their
mtime, and invalid input cannot replace the previous generation. Other profiles
retain their existing Make producer.

Build and post-build validation are separate CI steps. The source graph is
checked for DEB outputs and packaging commands before execution. Content checks
cover parent/source bytes, import records, permissions, debug correspondence,
platform, manifest labels and archive tags. Loaded probes use network-isolated
containers and private writable state, run the real entrypoint for single and
multi-instance configurations, verify 100 Redis databases and source sonic-db-cli
writes, and exercise the installed DASH CLI through its native library. Template
checks cover conditional event DBs, multi-ASIC global includes and chassis
selection. They preserve legacy optional BMP instance definitions without
expanding the migration scope to the BMP container.

No-op and template edit/restore checks verify unchanged parent/source TARs and
exact restored archives. Cross-machine image reproducibility remains dependent
on the separately tracked foreign-source path fixes. Full VS installer, hardware,
and chassis/namespace network acceptance are separate stages.

Refresh allowed external Debian inputs with the dedicated refresh module's
upstream rules_distroless apt.install graph, then run export_lock.py. Keep its
package lock in Git; keep generated MODULE.bazel.lock out of Git.
