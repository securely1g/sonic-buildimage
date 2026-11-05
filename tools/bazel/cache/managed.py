#!/usr/bin/env python3
"""Select the runner-managed anonymous cache without publishing its origin."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


FLAG = "SONIC_BAZEL_MANAGED_CACHE"
DOWNLOADER_FLAG = "SONIC_BAZEL_DOWNLOADER_CONFIG"
CONFIG = Path("/run/sonic-worker-cache/credential.json")
HELPER = Path("/opt/sonic-runner/cache_credentials.py")
BAZEL = "/usr/local/bin/bazel"
RUNTIME_FILES = (
    CONFIG,
    HELPER,
    Path("/opt/sonic-runner/bazel_launcher.py"),
    Path(BAZEL),
    Path("/usr/local/lib/bazel-8.5.1"),
    Path("/etc/ssl/certs/java/cacerts"),
)
RUNTIME_DIRECTORIES = (Path("/opt/sonic-runner/bazel-cache-compat"),)
ERROR = "Runner-managed Bazel cache selection failed."


def enabled():
    value = os.environ.get(FLAG, "")
    if value not in ("", "1"):
        raise ValueError(ERROR)
    return value == "1"


def _helper(command, request=None):
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", str(HELPER), command],
        input=None if request is None else json.dumps(request),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=15, check=False,
    )
    if result.returncode:
        raise ValueError(ERROR)
    return json.loads(result.stdout)


def _settings():
    """Keep the validated origin confined to this process and the installed helper."""
    try:
        if not enabled() or not os.path.lexists(CONFIG):
            raise ValueError(ERROR)
        settings = _helper("settings")
        if not isinstance(settings, dict) or set(settings) != {"endpoint", "host"}:
            raise ValueError(ERROR)
        endpoint, host = settings["endpoint"], settings["host"]
        if not isinstance(endpoint, str) or not isinstance(host, str):
            raise ValueError(ERROR)
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or parsed.hostname != host or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path != "/" or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError(ERROR)
        # This proof deliberately uses the configured anonymous mode. Never
        # forward a legacy or host credential to a newly selected origin.
        request = {"uri": endpoint + "cas/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
        if _helper("get", request) != {"headers": {}}:
            raise ValueError(ERROR)
        return endpoint, host
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise ValueError(ERROR) from None


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_identity():
    files = {}
    for path in RUNTIME_FILES:
        if path == CONFIG:
            continue
        if not path.is_file():
            raise ValueError(ERROR)
        files[str(path)] = _file_digest(path)
    for directory in RUNTIME_DIRECTORIES:
        if not os.path.lexists(directory):
            files[str(directory)] = None
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(ERROR)
        files[str(directory)] = "directory"
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(ERROR)
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(ERROR)
            files[str(path)] = _file_digest(path)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _public_selection(endpoint):
    return {
        "cache_mode": "runner-managed-anonymous",
        "cache_identity_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "cache_runtime_sha256": _runtime_identity(),
    }


def selection():
    """Return only the identities safe to retain with producer/consumer evidence."""
    endpoint, _ = _settings()
    return _public_selection(endpoint)


def runtime_paths():
    """Use the same managed launcher and cache policy inside the Make slave."""
    selection()
    if any(not path.is_file() for path in RUNTIME_FILES):
        raise ValueError(ERROR)
    directories = tuple(path for path in RUNTIME_DIRECTORIES if path.is_dir())
    return (*tuple(map(str, (*RUNTIME_FILES, *directories))), downloader_path())


def downloader_path():
    value = os.environ.get(DOWNLOADER_FLAG, "")
    path = Path(value)
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in path.parts
            or path.is_symlink() or not path.is_file()):
        raise ValueError(ERROR)
    return value


def downloader_option():
    return "--experimental_downloader_config=" + downloader_path()


def check_options(arguments):
    """Do not let job flags replace the launcher's selected anonymous origin."""
    if not enabled():
        return
    forbidden = {"--remote_cache", "--credential_helper", "--remote_header",
                 "--remote_cache_header", "--config"}
    if any(argument.split("=", 1)[0] in forbidden for argument in arguments):
        raise ValueError(ERROR)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(endpoint):
    request = urllib.request.Request(
        endpoint + "cas/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        method="HEAD")
    opener = urllib.request.build_opener(NoRedirects())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise ValueError(ERROR)
            return {"cache_access": True, "http_status": 200}
        except (OSError, urllib.error.URLError, ValueError) as error:
            transient = (isinstance(error, urllib.error.HTTPError) and error.code in (429, 502, 503, 504)
                         or isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)
                         or isinstance(error, TimeoutError))
            if attempt < 2 and transient:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                time.sleep(2 * (attempt + 1))
                continue
            result = {"cache_access": False, "error_type": type(error).__name__}
            if isinstance(error, urllib.error.HTTPError):
                result["http_status"] = error.code
            elif isinstance(error, urllib.error.URLError):
                reason = error.reason
                result["reason_type"] = type(reason).__name__
                if isinstance(getattr(reason, "errno", None), int):
                    result["errno"] = reason.errno
                if isinstance(reason, ssl.SSLCertVerificationError):
                    result["tls_verify_code"] = reason.verify_code
            return result
    raise AssertionError("unreachable")


def prepare(output, *, github_mask=False):
    endpoint, host = _settings()
    receipt = _public_selection(endpoint)
    if github_mask:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError(ERROR)
        # Register masks before any Bazel process can echo its effective options.
        print("::add-mask::" + endpoint, flush=True)
        print("::add-mask::" + host, flush=True)
    receipt.update(_probe(endpoint))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if not receipt["cache_access"]:
        raise ValueError(ERROR)
    print("Validated runner-managed anonymous cache access", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-mask", action="store_true")
    args = parser.parse_args()
    try:
        prepare(args.output, github_mask=args.github_mask)
    except (OSError, ValueError):
        print(ERROR, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
