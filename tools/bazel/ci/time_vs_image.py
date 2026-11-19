"""Record wall time and Bazel profiles for the VS installer's Make recipes."""

import argparse
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def start_recipe(workspace, owner, bazel, profiles, exports):
    root = workspace / "artifacts/vs/image-timings" / owner
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=root, prefix="recipe-"))
    (directory / "recipe.json").write_text(json.dumps({
        "bazel": bazel, "profiles": profiles, "exports": exports,
        "telemetry": [phase for phase in profiles if phase in ("build", "test")],
    }) + "\n")
    return directory


def exit_status(returncode):
    return returncode if returncode >= 0 else 128 - returncode


def telemetry_flags(directory, phase):
    return [f"--build_event_json_file={directory / (phase + '.bep.json')}",
            f"--execution_log_json_file={directory / (phase + '.execution.json')}"]


def compress_telemetry(directory, phase):
    for kind in ("bep", "execution"):
        path = directory / f"{phase}.{kind}.json"
        temporary = path.with_suffix(".json.gz.tmp")
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=1) as output:
                shutil.copyfileobj(source, output)
            temporary.replace(path.with_suffix(".json.gz"))
            path.unlink()
        except OSError as error:
            print(f"Telemetry compression unavailable for {phase}/{kind}: {error}", file=sys.stderr)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def time_phase(directory, phase):
    if directory is None:
        yield
        return
    start_epoch = time.time()
    start = time.monotonic()
    code = 1
    try:
        yield
        code = 0
    except subprocess.CalledProcessError as error:
        code = exit_status(error.returncode)
        raise
    except FileNotFoundError:
        code = 127
        raise
    finally:
        record = {"phase": phase, "start_epoch_seconds": start_epoch,
                  "elapsed_seconds": time.monotonic() - start, "exit_code": code}
        (directory / f"{phase}.timing.json").write_text(json.dumps(record) + "\n")


def main(arguments):
    if arguments[0] == "start-recipe":
        parser = argparse.ArgumentParser()
        parser.add_argument("workspace", type=Path)
        parser.add_argument("owner")
        for key in ("bazel", "profiles", "exports"):
            parser.add_argument("--" + key, nargs="+", required=True)
        args = parser.parse_args(arguments[1:])
        print(start_recipe(args.workspace, args.owner, args.bazel, args.profiles, args.exports))
        return 0
    if arguments[0] == "image-recipe":
        images = arguments[1:]
        commands = ["build", *("cquery-" + image for image in images)]
        directory = start_recipe(Path.cwd(), images[0], commands, commands,
                                 ["export-" + image for image in images])
        print(directory)
        return 0
    directory, phase, *command = arguments
    directory = Path(directory)
    if phase in ("build", "test") or phase.startswith("cquery-"):
        # cquery's stdout is consumed by Make. Only the real command writes there.
        profile = directory / f"{phase}.profile.json.gz"
        # Nested workspaces can have Bazel startup options before the command.
        index = command.index(phase.split("-")[0], 1) + 1
        command[index:index] = [f"--profile={profile}", "--noslim_profile"]
        if phase in ("build", "test"):
            command[index + 2:index + 2] = telemetry_flags(directory, phase)
    try:
        with time_phase(directory, phase):
            subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        return exit_status(error.returncode)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 127
    finally:
        if phase in ("build", "test"):
            compress_telemetry(directory, phase)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
