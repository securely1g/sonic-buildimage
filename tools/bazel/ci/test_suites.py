#!/usr/bin/env python3
"""Select ordinary TAR checks or explicitly requested Debian-package fixtures."""

import argparse
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
SUITES = ("tar-runtime", "deb-fixtures")

# These classes create real packages in setUp, including the inventory and
# publication tests that reuse KernelBundleTest. Keep their configuration and
# workflow siblings in normal CI. New package-producing tests belong here too.
DEB_FIXTURE_CLASSES = frozenset({
    ("kernel_test", "KernelBundleTest"),
    ("kernel_sbom_test", "KernelSbomTest"),
    ("kernel_artifacts_test", "KernelArtifactsTest"),
})

IMAGE_TESTS = (
    "//tools/bazel/image:assembly_formats_test",
    "//tools/bazel/image:mkinitramfs_test",
    "//tools/bazel/image/tar_split_json:format_test",
    "//tools/bazel/image/action_test:action_test",
)
KERNEL_DEB_TEST = "//tools/bazel/image:kernel_deb_test"
DISK_TEST = "//tools/bazel/image/action_test:disk_action_test"


def check_suite(suite):
    if suite not in SUITES:
        raise ValueError("unknown test suite: " + suite)


def test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def is_deb_fixture(test):
    cls = type(test)
    return (cls.__module__.rsplit(".", 1)[-1], cls.__name__) in DEB_FIXTURE_CLASSES


def select_unit_tests(discovered, suite):
    """Retain discovery failures and every test outside the producing classes."""
    check_suite(suite)
    return unittest.TestSuite(
        test for test in test_cases(discovered)
        if suite == "deb-fixtures" or not is_deb_fixture(test)
    )


def image_targets(suite, architecture):
    check_suite(suite)
    if architecture not in ("amd64", "arm64"):
        raise ValueError("unsupported image-test architecture: " + architecture)
    targets = list(IMAGE_TESTS)
    if suite == "deb-fixtures":
        targets.insert(2, KERNEL_DEB_TEST)
    if architecture == "amd64":
        targets.append(DISK_TEST)
    return targets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES,
                        default=os.environ.get("SONIC_BAZEL_CI_SUITE", "tar-runtime"))
    commands = parser.add_subparsers(dest="command", required=True)
    unit = commands.add_parser("unit", help="run discovered Make/OCI helper tests")
    unit.add_argument("--list", action="store_true", help="list selection without executing tests")
    image = commands.add_parser("image-targets", help="list image action test labels")
    image.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args(argv)
    # argparse does not validate defaults supplied by the environment.
    try:
        check_suite(args.suite)
    except ValueError as error:
        parser.error(str(error))
    if args.command == "image-targets":
        print("\n".join(image_targets(args.suite, args.architecture)))
        return 0
    sys.path.insert(0, str(ROOT))
    discovered = unittest.TestLoader().discover(str(ROOT / "tools/bazel/tests"), pattern="*_test.py")
    selected = select_unit_tests(discovered, args.suite)
    if args.list:
        print("\n".join(test.id() for test in test_cases(selected)))
        return 0
    print(f"CI suite: {args.suite}; {selected.countTestCases()} helper tests", flush=True)
    return 0 if unittest.TextTestRunner(verbosity=1).run(selected).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
