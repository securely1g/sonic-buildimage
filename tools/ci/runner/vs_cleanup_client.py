#!/usr/bin/env python3
"""Durably enroll one VS job with the canary's host supervisor."""

import argparse
import json
from pathlib import Path
import socket


SOCKET = "/run/sonic-vs-cleanup/control.sock"
LIMIT = 65536


def request(message, path=SOCKET, timeout=30):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as stream:
            response = stream.readline(LIMIT + 1)
        if not response.endswith(b"\n") or len(response) > LIMIT:
            raise ValueError("Invalid supervisor response")
        result = json.loads(response)
        if (set(result) != {"ok", "defer"} or result["ok"] is not True
                or not isinstance(result["defer"], bool)):
            raise ValueError("Supervisor refused the handoff")
        return result["defer"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enroll", "authorize"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--artifact-id", action="append", default=[])
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    output = vars(args).pop("github_output")
    try:
        deferred = request(vars(args))
        if args.action == "enroll" and output:
            with output.open("a") as stream:
                stream.write(f"enrolled={str(deferred).lower()}\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"VS cleanup handoff failed: {error}\n")


if __name__ == "__main__":
    main()
