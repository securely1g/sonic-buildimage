#!/usr/bin/env python3
"""Configure the runner's reviewed cache helper, allowing explicit local fallback."""

import argparse
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


RUNTIME_ROOT = Path("/opt/sonic-runner")
PROBE_TIMEOUT = 8
FALLBACK_BEGIN = "# BEGIN SONiC runner cache connectivity fallback"
FALLBACK_END = "# END SONiC runner cache connectivity fallback"


class ConfigurationFailure(Exception):
    def __init__(self, error_class, http_status=None):
        super().__init__(error_class)
        self.error_class = error_class
        self.http_status = http_status


def load_credentials():
    # Use the image's reviewed origin and header validation, including when
    # PYTHONSAFEPATH excludes the script directory from module discovery.
    spec = importlib.util.spec_from_file_location(
        "sonic_runner_cache_credentials", RUNTIME_ROOT / "cache_credentials.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response_mode(status):
    if status == 200:
        return {"mode": "local_and_remote", "error_class": None, "http_status": status}
    if 500 <= status <= 599:
        return {"mode": "local_only", "error_class": "remote_unavailable", "http_status": status}
    if status in (401, 403):
        raise ConfigurationFailure("authentication_rejected", status)
    if 300 <= status <= 399:
        raise ConfigurationFailure("redirect_refused", status)
    raise ConfigurationFailure("unexpected_http_status", status)


def check_remote(credentials, authorization):
    # Do not read the response body or follow redirects with the job's header.
    request = urllib.request.Request(
        credentials.ENDPOINT.rstrip("/") + "/status", method="GET",
        headers={"Authorization": authorization})
    opener = urllib.request.build_opener(credentials.NoRedirects())
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            return response_mode(response.status)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return response_mode(status)
    except urllib.error.URLError as error:
        code = "tls_error" if isinstance(error.reason, ssl.SSLError) else "transport_error"
    except ssl.SSLError:
        code = "tls_error"
    except OSError:
        code = "transport_error"
    return {"mode": "local_only", "error_class": code, "http_status": None}


def set_local_fallback(rc, enabled):
    contents = rc.read_text()
    if FALLBACK_BEGIN in contents or FALLBACK_END in contents:
        if (contents.count(FALLBACK_BEGIN) != 1 or contents.count(FALLBACK_END) != 1
                or contents.index(FALLBACK_BEGIN) > contents.index(FALLBACK_END)):
            raise ConfigurationFailure("configuration_failed")
        start = contents.index(FALLBACK_BEGIN)
        end = contents.index(FALLBACK_END) + len(FALLBACK_END)
        contents = contents[:start] + contents[end:].lstrip("\n")
    if enabled:
        # This last value overrides the public remote URL while keeping the
        # existing disk/repository caches, origin helper, and trust settings.
        contents = contents.rstrip("\n") + "\n\n" + "\n".join([
            FALLBACK_BEGIN,
            "# The remote cache was unavailable during this job's preflight.",
            "common --remote_cache=",
            FALLBACK_END,
        ]) + "\n"
    rc.write_text(contents)


def configure(cache_dir, rc):
    try:
        credentials = load_credentials()
    except Exception:
        raise ConfigurationFailure("runtime_helper_unavailable") from None
    try:
        authorization = credentials.authorization()
    except ValueError:
        raise ConfigurationFailure("credentials_invalid") from None
    result = check_remote(credentials, authorization)
    try:
        # The child inherits the step-scoped secret in its environment. Never
        # put it in arguments, configuration, captured logs, or the receipt.
        subprocess.run(
            [sys.executable, str(RUNTIME_ROOT / "cache_probe.py"), "configure",
             "--cache-dir", str(cache_dir), "--rc", str(rc)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30)
        set_local_fallback(rc, result["mode"] == "local_only")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ConfigurationFailure("configuration_failed") from None
    return {"status": "configured", **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("/bazel_cache"))
    parser.add_argument("--rc", type=Path, default=Path.home() / ".bazelrc")
    parser.add_argument("--output", type=Path, default=Path("artifacts/vs/cache-configuration.json"))
    args = parser.parse_args(argv)
    try:
        receipt = configure(args.cache_dir, args.rc)
    except ConfigurationFailure as error:
        receipt = {"status": "failure", "mode": "unconfigured",
                   "error_class": error.error_class, "http_status": error.http_status}
    except Exception:
        # Exception messages can contain request headers or helper output.
        receipt = {"status": "failure", "mode": "unconfigured",
                   "error_class": "configuration_failed", "http_status": None}
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except OSError:
        receipt = {"status": "failure", "mode": "unconfigured",
                   "error_class": "receipt_write_failed", "http_status": None}
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "configured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
