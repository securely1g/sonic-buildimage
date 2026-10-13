"""Install tested wheels away from source while retaining declared test dependencies."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import venv

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile


@dataclass(frozen=True)
class InstalledWheels:
    root: Path
    python: Path
    site: Path
    scripts: Path
    env: dict[str, str]


@contextmanager
def installed_wheels(wheels, *, exclude_source_roots):
    """Install wheels into a private venv, excluding owner checkout/runfiles paths.

    Tests may borrow declared third-party dependencies from their runfiles. The
    caller identifies source roots that must be excluded and checks that its
    component imports resolve inside ``site``. This fixture does not establish
    that the wheel's dependency closure is independently installable.
    """
    excluded = [Path(path).resolve() for path in exclude_source_roots]
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
        external = [str(Path(path).resolve()) for path in sys.path if path and
                    Path(path).is_dir() and not any(Path(path).resolve().is_relative_to(own)
                                                   for own in excluded)]
        (site / "declared-dependencies.pth").write_text("\n".join(external) + "\n")
        destination = SchemeDictionaryDestination(
            scheme_dict={key: scheme[key] for key in ("purelib", "platlib", "scripts", "data")} |
                        {"headers": scheme["include"]},
            interpreter=str(python), script_kind="posix", bytecode_optimization_levels=(),
        )
        for archive in wheels:
            with WheelFile.open(archive) as source:
                install(source, destination, additional_metadata={"INSTALLER": b"Bazel test\n"})
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        yield InstalledWheels(root, python, site, Path(scheme["scripts"]), env)


def write_wheel_receipt(wheels, *, required=False):
    """Record the tested component wheel bytes only after all its probes pass."""
    directory = (os.environ["TEST_UNDECLARED_OUTPUTS_DIR"] if required else
                 os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR"))
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / "wheels.json").write_text(json.dumps({
            "status": "passed", "python": platform.python_version(),
            "architecture": platform.machine(), "wheels": [{
                "filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            } for wheel in wheels],
        }, indent=2) + "\n")
