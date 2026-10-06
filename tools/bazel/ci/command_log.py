"""Retain command output and timings separately from queried artifact paths."""

import subprocess
import time


def execute(command, directory, receipt, name, *, cwd):
    """Retain diagnostics separately so they cannot be mistaken for output paths."""
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    (directory / (name + ".log")).write_text(result.stdout + result.stderr)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    receipt["commands"].append({"argv": command, "returncode": result.returncode,
                                "elapsed_seconds": time.monotonic() - started,
                                "log": name + ".log"})
    result.check_returncode()
    return result.stdout
