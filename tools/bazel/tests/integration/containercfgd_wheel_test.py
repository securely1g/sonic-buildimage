"""Install the wheel and exercise its command without Redis or system writes."""

from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import tempfile
from unittest import mock
import venv
import zipfile

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
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

    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as temporary:
        root = Path(temporary)
        environment = root / "environment"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        python = environment / "bin/python"
        scheme = json.loads(subprocess.check_output(
            [str(python), "-I", "-c", "import json,sysconfig;print(json.dumps(sysconfig.get_paths()))"],
            text=True))
        site = Path(scheme["purelib"])
        site.mkdir(parents=True, exist_ok=True)
        # Only external declared dependencies may come from Bazel runfiles;
        # containercfgd and py-common must come from the installed wheels.
        own_roots = [Path(__file__).absolute().parents[4], Path(__file__).resolve().parents[4]]
        external = [str(Path(path).resolve()) for path in sys.path if path and
                    Path(path).is_dir() and not any(Path(path).resolve().is_relative_to(
                        own.resolve()) for own in own_roots)]
        (site / "declared-dependencies.pth").write_text("\n".join(external) + "\n")
        destination = SchemeDictionaryDestination(
            scheme_dict={key: scheme[key] for key in ("purelib", "platlib", "scripts", "data")} |
                        {"headers": scheme["include"]},
            interpreter=str(python), script_kind="posix", bytecode_optimization_levels=(),
        )
        for archive in (common, wheel):
            with WheelFile.open(archive) as source:
                install(source, destination, additional_metadata={"INSTALLER": b"Bazel test\n"})
        for name, source in sources.items():
            assert (site / name).read_bytes() == source.read_bytes(), name
        executable = environment / "bin/containercfgd"
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
        env = dict(os.environ, CONTAINER_NAME="swss", NAMESPACE_ID="")
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        entries = dict(value.split(" = ", 1) for value in contract["entry_points"]["console_scripts"])
        subprocess.run([str(python), "-I", "-c", probe, str(site), str(executable),
                        json.dumps(entries), contract["version"]],
                       cwd=root, env=env, check=True, timeout=30)
    output = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / "wheels.json").write_text(json.dumps({
            "status": "passed", "python": platform.python_version(),
            "architecture": platform.machine(), "wheels": [{
                "filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }],
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
