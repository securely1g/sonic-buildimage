"""Install both SONiC wheels and check their setup.py package contracts."""

import argparse
from contextlib import contextmanager
from email.parser import BytesParser
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import venv
import zipfile

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pkg_resources
import setuptools


parser = argparse.ArgumentParser()
for option in ("common-wheel", "config-wheel", "common-setup", "config-setup"):
    parser.add_argument("--" + option, type=Path, required=True)
INPUTS, TEST_ARGS = parser.parse_known_args()
RECEIPT = {"architecture": platform.machine(), "python": platform.python_version()}


@contextmanager
def source_context(directory):
    previous_cwd, previous_path = Path.cwd(), sys.path[:]
    try:
        os.chdir(directory)
        sys.path.insert(0, str(directory))
        yield
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = previous_path


def setup_contract(path):
    """Read setup.py's selected Python 3 metadata without building or installing."""
    captured = {}

    def distribution(name):
        # setup.py checks preinstalled prerequisites before declaring metadata.
        # These checks do not affect the selected package payload. Use pinned
        # declared dependencies when present; SONiC distributions are source deps.
        project = pkg_resources.Requirement.parse(name).project_name
        try:
            version = importlib.metadata.version(project)
        except importlib.metadata.PackageNotFoundError:
            if canonicalize_name(project) not in {
                "sonic-py-common", "sonic-yang-mgmt", "sonic-yang-models",
            }:
                raise
            version = "1.0"
        return types.SimpleNamespace(version=version)

    with source_context(path.parent), mock.patch.object(setuptools, "setup", side_effect=lambda **kwargs: captured.update(kwargs)), mock.patch.object(pkg_resources, "get_distribution", side_effect=distribution):
        runpy.run_path(str(path), run_name="__wheel_contract__")
    if not captured:
        raise AssertionError(f"No setup metadata captured from {path}")
    return captured


def normalized_requirement(text):
    requirement = Requirement(text)
    return (canonicalize_name(requirement.name), str(requirement.specifier),
            str(requirement.marker) if requirement.marker else "")


def expected_sources(contract, root):
    result = {name + ".py": root / (name + ".py") for name in contract.get("py_modules", [])}
    for package in contract.get("packages", []):
        directory = root / package.replace(".", "/")
        sources = list(directory.glob("*.py"))
        if not sources:
            raise AssertionError(f"Package has no declared Python files: {package}")
        result.update({str(source.relative_to(root)): source for source in sources})
    return result


def dependency_paths():
    """Keep declared external dependencies; remove this repository's sources."""
    own_roots = [Path(__file__).absolute().parents[4], Path(__file__).resolve().parents[4]]
    paths = []
    for item in sys.path:
        if not item:
            continue
        path = Path(item).resolve()
        if not path.is_dir() or any(path.is_relative_to(root.resolve()) for root in own_roots):
            continue
        if str(path) not in paths:
            paths.append(str(path))
    return paths


class InstalledWheelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="installed-sonic-wheels-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.environment = cls.root / "environment"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(cls.environment)
        cls.python = cls.environment / "bin/python"
        cls.env = dict(os.environ, PLATFORM="sonic-bazel-build", NAMESPACE_ID="")
        for key in ("PYTHONPATH", "PYTHONHOME", "CFGGEN_UNIT_TESTING", "CFGGEN_UNIT_TESTING_TOPOLOGY"):
            cls.env.pop(key, None)
        cls.env["PATH"] = str(cls.environment / "bin") + os.pathsep + cls.env.get("PATH", "")
        process = subprocess.run([str(cls.python), "-I", "-c", "import json,sysconfig; print(json.dumps(sysconfig.get_paths()))"],
                                 cwd=cls.root, env=cls.env, check=True, capture_output=True, text=True)
        scheme = json.loads(process.stdout)
        cls.site = Path(scheme["purelib"])
        cls.site.mkdir(parents=True, exist_ok=True)
        cls.external_paths = dependency_paths()
        (cls.site / "declared-dependencies.pth").write_text("\n".join(cls.external_paths) + "\n")
        destination = SchemeDictionaryDestination(
            scheme_dict={key: scheme[key] for key in ("purelib", "platlib", "headers", "scripts", "data") if key in scheme} | {"headers": scheme["include"]},
            interpreter=str(cls.python), script_kind="posix", bytecode_optimization_levels=(),
        )
        cls.packages = []
        for kind in ("common", "config"):
            wheel = getattr(INPUTS, kind + "_wheel").absolute()
            setup = getattr(INPUTS, kind + "_setup").absolute()
            contract = setup_contract(setup)
            with WheelFile.open(wheel) as source:
                install(source, destination, additional_metadata={"INSTALLER": b"SONiC Bazel wheel test\n"})
            cls.packages.append((wheel, setup, contract))
        RECEIPT["wheels"] = [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                              "distribution": contract["name"], "version": contract["version"]}
                             for wheel, _, contract in cls.packages]

    def test_metadata_and_payload_match_setup(self):
        """Ship every declared module, command and template with matching metadata."""
        for wheel, setup, contract in self.packages:
            with self.subTest(distribution=contract["name"]), zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                sources = expected_sources(contract, setup.parent)
                actual_python = {name for name in names if name.endswith(".py") and ".dist-info/" not in name and ".data/" not in name}
                self.assertEqual(actual_python, set(sources))
                for member, source in sources.items():
                    self.assertEqual(archive.read(member), source.read_bytes(), member)
                    self.assertEqual((self.site / member).read_bytes(), source.read_bytes(), member)
                metadata_file = next(name for name in names if name.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(metadata_file))
                self.assertEqual(canonicalize_name(metadata["Name"]), canonicalize_name(contract["name"]))
                self.assertEqual(metadata["Version"], contract["version"])
                actual_requires = {normalized_requirement(value) for value in metadata.get_all("Requires-Dist", [])}
                expected_requires = {normalized_requirement(value) for value in contract["install_requires"]}
                for extra, requirements in contract.get("extras_require", {}).items():
                    expected_requires.update(normalized_requirement(value + f'; extra == "{extra}"') for value in requirements)
                self.assertEqual(actual_requires, expected_requires)
                for directory, files in contract.get("data_files", []):
                    for name in files:
                        relative = Path(directory.lstrip("/")) / Path(name).name
                        # bdist_wheel puts absolute setup.py data_files paths
                        # directly at the archive root, hence under purelib
                        # after installation. Relative paths use the data scheme.
                        if Path(directory).is_absolute():
                            member = str(relative)
                            installed = self.site / relative
                        else:
                            prefix = metadata_file.split(".dist-info/", 1)[0]
                            member = prefix + ".data/data/" + str(relative)
                            installed = self.environment / relative
                        self.assertEqual(archive.read(member), (setup.parent / name).read_bytes())
                        self.assertEqual(installed.read_bytes(), (setup.parent / name).read_bytes())
                for name in contract.get("scripts", []):
                    installed = self.environment / "bin" / Path(name).name
                    self.assertTrue(os.access(installed, os.X_OK), str(installed))
                    self.assertEqual(installed.read_bytes().split(b"\n", 1)[1], (setup.parent / name).read_bytes().split(b"\n", 1)[1])
                expected_entries = contract.get("entry_points", {}).get("console_scripts", [])
                if expected_entries:
                    import configparser
                    entry_file = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
                    entries = configparser.ConfigParser()
                    entries.read_string(archive.read(entry_file).decode())
                    self.assertEqual(dict(entries["console_scripts"]), dict(line.split(" = ", 1) for line in expected_entries))
                for name in contract.get("license_files", []):
                    member = next(member for member in names if member.endswith("/licenses/" + name))
                    self.assertEqual(archive.read(member), (setup.parent / name).read_bytes())

    def test_installed_imports_and_commands(self):
        """Run both installed packages with their source directories unavailable."""
        modules = []
        for _, setup, contract in self.packages:
            modules.extend(name.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                           for name in expected_sources(contract, setup.parent))
        probe = """import hashlib,importlib,importlib.metadata,json,pathlib,sys
site=pathlib.Path(sys.argv[1]).resolve()
result={}
for distribution in ("sonic-py-common", "sonic-config-engine"):
    metadata=importlib.metadata.distribution(distribution)
    assert metadata.version=="1.0", (distribution,metadata.version)
    assert pathlib.Path(metadata.locate_file("")).resolve()==site, distribution
for name in json.loads(sys.argv[2]):
    module=importlib.import_module(name)
    path=pathlib.Path(module.__file__).resolve()
    assert path.is_relative_to(site), (name,str(path),str(site))
    result[name]=str(path.relative_to(site))
from sonic_grpc.gnoi import system_pb2
value=system_pb2.RebootRequest(method=system_pb2.HALT)
assert system_pb2.RebootRequest.FromString(value.SerializeToString()).method==system_pb2.HALT
import redisdl
# SONiC's existing redis-dump-load patch adds this batched pipeline reader.
# Empty input exercises that entry point without connecting to a Redis server.
assert list(redisdl._read_keys(None, [], pretty=False, encoding="utf-8"))==[]
redis_source=pathlib.Path(redisdl.__file__).read_bytes()
print(json.dumps({"modules": result, "redis_dump_load": {
    "version": importlib.metadata.version("redis-dump-load"),
    "source_sha256": hashlib.sha256(redis_source).hexdigest(),
    "pipeline_reader": True,
}},sort_keys=True))
"""
        process = subprocess.run([str(self.python), "-I", "-c", probe, str(self.site), json.dumps(sorted(set(modules)))],
                                 cwd=self.root, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        installed = json.loads(process.stdout)
        RECEIPT["installed_modules"] = installed["modules"]
        RECEIPT["redis_dump_load"] = installed["redis_dump_load"]
        for name, expected in (("sonic-cfggen", "--from-db"), ("sonic-db-load", "Load data from FILE"), ("sonic-db-dump", "Dump data from specified")):
            # Use the installed entrypoint (including its installer-selected
            # interpreter) from a directory with neither package's source tree.
            result = subprocess.run([str(self.environment / "bin" / name), "--help"], cwd=self.root,
                                    env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(expected, result.stdout)
        generated = subprocess.run([str(self.environment / "bin/sonic-cfggen"), "-a", '{"hostname":"installed-wheel"}', "-v", "hostname"],
                                   cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertEqual(generated.stdout.strip(), "installed-wheel")
        RECEIPT["commands"] = ["sonic-cfggen", "sonic-db-load", "sonic-db-dump"]


if __name__ == "__main__":
    result = unittest.main(argv=[__file__, *TEST_ARGS], exit=False).result
    RECEIPT.update(status="passed" if result.wasSuccessful() else "failed", tests_run=result.testsRun)
    output = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / "wheels.json").write_text(json.dumps(RECEIPT, indent=2, sort_keys=True) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
