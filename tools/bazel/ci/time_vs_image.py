"""Record one VS image recipe phase, passing command output back to Make."""

import json
from pathlib import Path
import subprocess
import sys
import time


def main(owner, phase, command):
    directory = Path("artifacts/vs/image-timings") / owner
    directory.mkdir(parents=True, exist_ok=True)
    if phase == "build" or phase.startswith("cquery-"):
        # The command can be bazel or the Bazel wrapper supplied by the slave.
        # Put the flags after the subcommand. cquery stdout must remain only paths.
        profile = (directory / f"{phase}.profile.json.gz").resolve()
        command = [*command[:2], f"--profile={profile}", "--noslim_profile", *command[2:]]

    start_epoch = time.time()
    start = time.monotonic()
    try:
        result = subprocess.run(command)
        code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        code = 127
    record = {
        "phase": phase,
        "start_epoch_seconds": start_epoch,
        "elapsed_seconds": time.monotonic() - start,
        "exit_code": code,
    }
    (directory / f"{phase}.timing.json").write_text(json.dumps(record) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
