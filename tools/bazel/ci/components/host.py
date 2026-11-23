"""Native VS host installation interfaces and the original tests that consume them."""

from importlib import import_module
import json
from pathlib import Path

from tools.bazel.ci import vs_python_packages


# Host packages belong to the installer rather than an OCI image.
IMAGES = ()
REUSABLE_WORKFLOW = True
RUNTIME = {
    "bash-tacplus.tar": "@bash_tacplus//:bash-tacplus.runtime",
    "bash-tacplus.control.tar": "@bash_tacplus//:bash-tacplus.control",
    "radius.tar": "//src/radius/pam:libpam-radius-auth.runtime",
    "radius.control.tar": "//src/radius/pam:libpam-radius-auth.control",
    "radius.json": "//src/radius/pam:runtime_metadata",
    "sonic-platform-vs_1.0_amd64.deb": "//platform/vs/sonic-platform-modules-vs:deb",
    "ctrmgrd.json": "//src/sonic-ctrmgrd:ctrmgrd.runtime_metadata",
    "ctrmgrd-rs.json": "//src/sonic-ctrmgrd-rs:runtime_packages.json",
    "device-data.json": "//src/sonic-device-data:runtime_packages.json",
    "rsyslog-plugin.json": "//src/sonic-eventd:bazel/rsyslog_plugin.runtime_packages.json",
    "rsyslog-plugin.tar": "//src/sonic-eventd:rsyslog_plugin_runtime",
    "nettools.json": "//src/sonic-nettools:runtime_packages.json",
    "syslog-counter.json": "//src/syslog-counter:runtime_packages.json",
    "system-health.json": "//src/system-health:system_health.runtime_metadata",
    "systemd-sonic-generator.json": "//src/systemd-sonic-generator:systemd-sonic-generator.runtime_metadata",
    "host-services.json": "@sonic_host_services//:sonic-host-services.runtime_metadata",
    "host-services-data.json": "@sonic_host_services//:sonic-host-services-data.runtime_metadata",
    "host-services-rs.json": "@sonic_host_services//:sonic-host-services-rs.runtime_metadata",
}
DEBUG = {"rsyslog-plugin.debug.tar": "//src/sonic-eventd:rsyslog_plugin_runtime.debug_symbols"}
TEST_INPUTS = {
    "@bash_tacplus//:test_payload": "bash-tacplus.tar",
    "@bash_tacplus//:test_control": "bash-tacplus.control.tar",
    "//src/radius/pam:test_payload": "radius.tar",
    "//src/radius/pam:test_control": "radius.control.tar",
    "//src/radius/pam:test_metadata": "radius.json",
    "//platform/vs/sonic-platform-modules-vs:test_deb": "sonic-platform-vs_1.0_amd64.deb",
}


def verify(directory, *, debug):
    checker = import_module("src.sonic-eventd.bazel.verify_packages")
    checker.verify(Path(checker.__file__).resolve().parents[1], {
        "rsyslog_plugin": (directory / "rsyslog-plugin.tar", directory / "rsyslog-plugin.debug.tar" if debug else None)
    }, "amd64")
    for filename in RUNTIME:
        if filename.endswith(".json"):
            if not json.loads((directory / filename).read_bytes()):
                raise ValueError("Empty native VS host package metadata: " + filename)


def stage(package, revision, debug):
    """Expose only files verified against this producer to the native Bazel tests."""
    from tools.bazel.ci import vs_component_artifacts

    package = vs_python_packages.package_path(package)
    vs_component_artifacts.verify_inputs("host", package, revision, debug)
    verify(package, debug=debug)
    (package / "BUILD.bazel").write_text("exports_files(" + json.dumps(sorted(TEST_INPUTS.values())) + ")\n")
    return [f"--{selector}=//{package}:{filename}" for selector, filename in TEST_INPUTS.items()]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--debug", choices=("y", "n"), required=True)
    args = parser.parse_args()
    print(*stage(args.package, args.revision, args.debug == "y"), sep="\n")
