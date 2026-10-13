"""Install the wheel and exercise its command without Redis or system writes."""

from email.parser import BytesParser
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest import mock
import zipfile

from tools.bazel.tests.integration.installed_wheel_fixture import installed_wheels, write_wheel_receipt
from packaging.utils import canonicalize_name
import setuptools


def main():
    wheel, setup, common = (Path(value).absolute() for value in sys.argv[1:])
    contract = {}
    with mock.patch.object(setuptools, "setup", side_effect=lambda **kw: contract.update(kw)):
        runpy.run_path(str(setup))
    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        sources = {str(path.relative_to(setup.parent)): path
                   for name in contract["packages"]
                   for path in (setup.parent / name).glob("*.py")}
        assert {name for name in names if name.endswith(".py")} == set(sources)
        for name, source in sources.items():
            assert package.read(name) == source.read_bytes(), name
        metadata = BytesParser().parsebytes(package.read(next(
            name for name in names if name.endswith(".dist-info/METADATA"))))
        assert canonicalize_name(metadata["Name"]) == canonicalize_name(contract["name"])
        assert metadata["Version"] == contract["version"]
        assert {canonicalize_name(value) for value in metadata.get_all("Requires-Dist", [])} == {
            canonicalize_name(value) for value in contract["install_requires"]}

    own_roots = [Path(__file__).absolute().parents[4], Path(__file__).resolve().parents[4]]
    with installed_wheels((common, wheel), exclude_source_roots=own_roots) as installed:
        root, python, site = installed.root, installed.python, installed.site
        for name, source in sources.items():
            assert (site / name).read_bytes() == source.read_bytes(), name
        executable = installed.scripts / "containercfgd"
        assert os.access(executable, os.X_OK)
        probe = """
import builtins, importlib.metadata, io, json, pathlib, runpy, sys
from unittest import mock
from swsscommon import swsscommon
site = pathlib.Path(sys.argv[1])
# Boot synchronization and Redis are operating-system services. Exercise the
# installed command while replacing only those external interactions.
system_open = builtins.open
def read_configuration(path, *args, **kwargs):
    if path == '/etc/rsyslog.conf':
        return io.StringIO('')
    return system_open(path, *args, **kwargs)
with mock.patch.object(swsscommon.RestartWaiter, 'waitAdvancedBootDone'), mock.patch.object(builtins, 'open', side_effect=read_configuration):
    from containercfgd import containercfgd
from sonic_py_common import daemon_base
for module in (containercfgd, daemon_base):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(site), module.__file__
distribution = importlib.metadata.distribution('sonic-containercfgd')
entries = {entry.name: entry.value for entry in distribution.entry_points}
assert entries == json.loads(sys.argv[3]), entries
assert distribution.version == sys.argv[4]
with mock.patch.object(containercfgd.ContainerConfigDaemon, 'run') as run:
    try:
        runpy.run_path(sys.argv[2], run_name='__main__')
    except SystemExit as result:
        assert result.code in (None, 0), result.code
    run.assert_called_once()
print('Installed containercfgd command and py-common imports passed')
"""
        env = dict(installed.env, CONTAINER_NAME="swss", NAMESPACE_ID="")
        entries = dict(value.split(" = ", 1) for value in contract["entry_points"]["console_scripts"])
        subprocess.run([str(python), "-I", "-c", probe, str(site), str(executable),
                        json.dumps(entries), contract["version"]],
                       cwd=root, env=env, check=True, timeout=30)
    write_wheel_receipt([wheel])


if __name__ == "__main__":
    main()
