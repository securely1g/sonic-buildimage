"""Install the source wheel and exercise the supervisor listener's public API."""

from email.parser import BytesParser
import os
from pathlib import Path
import runpy
import subprocess
import sys
import types
from unittest import mock
import zipfile

from tools.bazel.tests.integration.installed_wheel_fixture import installed_wheels, write_wheel_receipt
from packaging.utils import canonicalize_name
import pkg_resources
import setuptools


def main():
    wheel, setup, common = (Path(value).absolute() for value in sys.argv[1:])
    contract = {}
    original_distribution = pkg_resources.get_distribution

    def distribution(name):
        # setup.py checks the first-party wheel before describing this package.
        if canonicalize_name(name) == "sonic-py-common":
            return types.SimpleNamespace(version="1.0")
        return original_distribution(name)

    with mock.patch.object(setuptools, "setup", side_effect=lambda **kw: contract.update(kw)), \
            mock.patch.object(pkg_resources, "get_distribution", side_effect=distribution):
        runpy.run_path(str(setup))
    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        assert not contract["packages"]
        assert not any(name.endswith(".py") for name in names)
        metadata = BytesParser().parsebytes(package.read(next(
            name for name in names if name.endswith(".dist-info/METADATA"))))
        assert canonicalize_name(metadata["Name"]) == canonicalize_name(contract["name"])
        assert metadata["Version"] == contract["version"]
        assert {canonicalize_name(value) for value in metadata.get_all("Requires-Dist", [])} == {
            canonicalize_name(value) for value in contract["install_requires"]}
        assert contract["scripts"] == ["scripts/supervisor-proc-exit-listener"]
        member = next(name for name in names if name.endswith(".data/scripts/supervisor-proc-exit-listener"))
        script = setup.parent / contract["scripts"][0]
        assert package.read(member).split(b"\n", 1)[1] == script.read_bytes().split(b"\n", 1)[1]

    own_roots = [Path(__file__).absolute().parents[4], Path(__file__).resolve().parents[4]]
    with installed_wheels((common, wheel), exclude_source_roots=own_roots) as installed:
        root, python, site = installed.root, installed.python, installed.site
        executable = installed.scripts / "supervisor-proc-exit-listener"
        assert os.access(executable, os.X_OK)
        assert executable.read_bytes().split(b"\n", 1)[1] == script.read_bytes().split(b"\n", 1)[1]
        fixture = root / "critical-processes"
        fixture.write_text("group:services\nprogram:syncd\n")
        probe = """
import importlib.metadata, pathlib, runpy, sys
from sonic_py_common import daemon_base
site = pathlib.Path(sys.argv[1])
assert pathlib.Path(daemon_base.__file__).resolve().is_relative_to(site)
distribution = importlib.metadata.distribution('sonic-supervisord-utilities')
assert distribution.version == sys.argv[4]
namespace = runpy.run_path(sys.argv[2])
assert namespace['get_group_and_process_list'](sys.argv[3]) == (['services'], ['syncd'])
try:
    namespace['main']([])
except SystemExit as result:
    assert result.code == 1, result.code
else:
    raise AssertionError('The installed listener accepted a missing container name')
assert importlib.metadata.version('supervisor') == '4.3.0'
print('Installed listener, native imports and critical-process parsing passed')
"""
        env = installed.env
        subprocess.run([str(python), "-I", "-c", probe, str(site), str(executable),
                        str(fixture), contract["version"]], cwd=root, env=env, check=True, timeout=30)
        command = subprocess.run([str(executable)], cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert command.returncode == 1 and not command.stderr, command
    write_wheel_receipt([wheel])


if __name__ == "__main__":
    main()
