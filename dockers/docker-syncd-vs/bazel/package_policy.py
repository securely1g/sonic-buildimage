"""Shared Syncd policy for preparing and consuming the Make package handoff."""

import base_debug_symbols


FEATURES = {"include_vs_dash_sai": "y", "include_fips": "y", "enable_asan": "n", "enable_syncd_rpc": "n"}
DEBUG_APT_PACKAGES = frozenset({"gdb", "gdbserver", "sshpass", "strace", "vim"})
SOURCE_PACKAGES = frozenset({"libswsscommon", "libsairedis", "libsaimetadata"})
SOURCE_PACKAGE_NAMES = SOURCE_PACKAGES | {name + "-dbgsym" for name in SOURCE_PACKAGES}


def require_runtime_fips(records):
    """Runtime owns the FIPS OpenSSH package; debug must inherit that identity."""
    matches = [record for record in records if record.get("package") == "openssh-client"]
    if len(matches) != 1 or "+fips" not in matches[0].get("version", ""):
        raise ValueError("runtime package handoff requires the Make FIPS openssh-client")
    record = matches[0]
    fields = record.get("control_fields", {})
    if (fields.get("Package"), fields.get("Version"), fields.get("Architecture")) != (
            record["package"], record["version"], record.get("architecture")) or record.get("architecture") != "amd64":
        raise ValueError("runtime FIPS openssh-client identity differs from its Debian control")


def reject_source_packages(records, *, allow_base_symbols=False):
    """Reject Make copies of source libraries, except the filtered inherited symbols."""
    unexpected = set()
    for record in records:
        name = record.get("package")
        if name not in SOURCE_PACKAGE_NAMES:
            continue
        if allow_base_symbols and name == base_debug_symbols.PACKAGE:
            base_debug_symbols.check_record(record)
        else:
            unexpected.add(name)
    if unexpected:
        raise ValueError("Make handoff contains packages now built from source: " + ", ".join(sorted(unexpected)))
