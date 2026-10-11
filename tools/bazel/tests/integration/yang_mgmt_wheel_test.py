"""Install the wheel and validate real SONiC YANG data outside the source tree."""

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
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import setuptools


def main():
    wheel, setup, models = (Path(value).absolute() for value in sys.argv[1:])
    contract = {}
    previous = Path.cwd()
    try:
        os.chdir(setup.parent)
        with mock.patch.object(setuptools, "setup", side_effect=lambda **kw: contract.update(kw)):
            runpy.run_path(str(setup))
    finally:
        os.chdir(previous)
    sources = {name + ".py": setup.parent / (name + ".py") for name in contract["py_modules"]}
    sources.update({str(path.relative_to(setup.parent)): path for name in contract["packages"]
                    for path in (setup.parent / name).glob("*.py")})
    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        assert {name for name in names if name.endswith(".py")} == set(sources)
        for name, source in sources.items():
            assert package.read(name) == source.read_bytes(), name
        metadata = BytesParser().parsebytes(package.read(next(
            name for name in names if name.endswith(".dist-info/METADATA"))))
        assert canonicalize_name(metadata["Name"]) == canonicalize_name(contract["name"])
        assert metadata["Version"] == contract["version"]
        assert {str(Requirement(value)) for value in metadata.get_all("Requires-Dist", [])} == {
            str(Requirement(value)) for value in contract["install_requires"]}
        for script in contract["scripts"]:
            entry = next(name for name in names if name.endswith(".data/scripts/" + script))
            assert package.read(entry).splitlines()[1:] == (setup.parent / script).read_bytes().splitlines()[1:]

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
        own_roots = [Path(__file__).absolute().parents[4], Path(__file__).resolve().parents[4]]
        external = [str(Path(path).resolve()) for path in sys.path if path and Path(path).is_dir()
                    and not any(Path(path).resolve().is_relative_to(own.resolve()) for own in own_roots)]
        (site / "declared-dependencies.pth").write_text("\n".join(external) + "\n")
        destination = SchemeDictionaryDestination(
            scheme_dict={key: scheme[key] for key in ("purelib", "platlib", "scripts", "data")} |
                        {"headers": scheme["include"]},
            interpreter=str(python), script_kind="posix", bytecode_optimization_levels=(),
        )
        with WheelFile.open(wheel) as source:
            install(source, destination, additional_metadata={"INSTALLER": b"Bazel test\n"})
        probe = '''
import importlib.metadata, pathlib, sys
import sonic_yang, sonic_yang_ext, sonic_yang_path
site, models = map(pathlib.Path, sys.argv[1:])
for module in (sonic_yang, sonic_yang_ext, sonic_yang_path):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(site), module.__file__
assert importlib.metadata.version('sonic-yang-mgmt') == '1.0'
manager = sonic_yang.SonicYang(str(models), print_log_enabled=False)
assert manager.loadYangModel() is True
assert set(manager.yangFiles) == {path.stem for path in models.glob('*.yang')}
config = {'SYSLOG_SERVER': {'192.0.2.1': {'port': '514', 'protocol': 'udp', 'severity': 'info'}}}
manager.loadData(config, quiet=True)
manager.validate_data_tree()
assert manager.getData()['SYSLOG_SERVER'] == config['SYSLOG_SERVER']
assert not manager.tablesWithOutYang
path = '/SYSLOG_SERVER/192.0.2.1/port'
assert manager.xpath_to_configdb_path(manager.configdb_path_to_xpath(path)) == path
manager.root.free()
manager.root = None
try:
    manager.loadData({'SYSLOG_SERVER': {'192.0.2.1': {'port': '70000'}}}, quiet=True)
    manager.validate_data_tree()
except sonic_yang_ext.SonicYangException:
    pass
else:
    raise AssertionError('Invalid port bypassed installed YANG validation')
print('Installed wheel: production models, ConfigDB roundtrip, path translation and invalid-data rejection passed')
'''
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        subprocess.run([str(python), "-I", "-c", probe, str(site), str(models)],
                       cwd=root, env=env, check=True, timeout=120)
        script = environment / "bin/sonic-cfg-help"
        assert os.access(script, os.X_OK)
        output = subprocess.check_output([str(script), "--help"], cwd=root, env=env, text=True)
        assert "--table" in output and "--field" in output, output
    directory = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "wheels.json").write_text(json.dumps({
        "status": "passed", "python": platform.python_version(), "architecture": platform.machine(),
        "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
