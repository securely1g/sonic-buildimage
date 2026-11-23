"""Execute the pinned libteam consumer with the VS-built libteam runtime TARs."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from tools.bazel.ci.vs_component_artifacts import verify_inputs
from tools.bazel.ci.vs_python_packages import package_path


def stage(bazel, bundle, revision, debug, package):
    verify_inputs("swss", bundle, revision, debug)
    package = package_path(package)
    # Read canonical labels so dependencies private to the libteam module (including
    # its target sysroot) keep the exact repository mapping used by the owner.
    xml = subprocess.check_output([bazel, "query", "--consistent_labels", "--output=xml", "@libteam//:consumer_test"])
    rule = ET.fromstring(xml).find("rule")
    if rule is None or rule.get("class") != "native_runtime_test":
        raise ValueError("Expected the pinned native libteam consumer test")
    explicit = {item.get("name") for item in rule if item.tag != "rule-input"}
    if explicit - {"name", "binary", "runtime_payloads"}:
        raise ValueError("libteam consumer gained attributes that the installed test must preserve")
    owner, sep, name = rule.attrib["name"].rpartition(":")
    if not sep or name != "consumer_test":
        raise ValueError("Unexpected libteam test target: " + rule.attrib["name"])
    replacements = {owner + ":libteam_runtime": "libteam.tar", owner + ":libteamdctl_runtime": "libteamdctl.tar"}
    payloads = [node.attrib["value"] for node in rule.findall("list[@name='runtime_payloads']/label")]
    binary = rule.find("label[@name='binary']")
    if not replacements.keys() <= set(payloads) or binary is None:
        raise ValueError("libteam consumer changed its declared binary or runtime")
    package.mkdir()
    for filename in replacements.values():
        shutil.copyfile(bundle / filename, package / filename)
    args = [":" + replacements[label] if label in replacements else label for label in payloads]
    (package / "BUILD.bazel").write_text(
        'load("@sonic_build_infra//cc:runtime_test.bzl", "native_runtime_test")\n'
        + 'native_runtime_test(name="consumer_test", binary=' + json.dumps(binary.attrib["value"])
        + ', runtime_payloads=' + json.dumps(args) + ')\n')
    return f"//{package}:consumer_test"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", required=True)
    parser.add_argument("--vs-swss", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--debug", choices=("y", "n"), required=True)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    print(stage(args.bazel, args.vs_swss, args.revision, args.debug == "y", args.package))
