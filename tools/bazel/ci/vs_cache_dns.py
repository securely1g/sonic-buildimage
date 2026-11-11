"""Resolve bounded cache/proxy/control roles; never perform an HTTP request."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
# Keep tools.* unimported until the selected native checkout is on sys.path.
# Importing the observer's tools package first could bind the later native import
# to the wrong checkout. The reused adapter itself imports only the stdlib.
TRANSPORT_PATH = ROOT / "tools/bazel/ci/vs_cache_transport.py"
with TRANSPORT_PATH.open("rb") as source:
    adapter_bytes = source.read(65537)
if hashlib.sha256(adapter_bytes).hexdigest() != "4fdf103fa9d4cca3279a6b8063789f995fd239267a57a3bc420c8ea454333a35":
    raise ValueError("Observer transport source binding differs")
_spec = importlib.util.spec_from_file_location("_vs_cache_dns_transport", TRANSPORT_PATH)
transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transport)

CACHE_HOST = "lgh-bazel-87263989.eastus.cloudapp.azure.com"
CONTROL_HOST = "api.github.com"
ERROR_TYPES = frozenset(("gaierror", "TimeoutError", "OSError", "UnicodeError", "ValueError", "unclassified"))
require = transport.require


def selected_hosts():
    # These are the same environment selection, parse, bypass and unquote
    # operations as urllib ProxyHandler.proxy_open for an HTTPS request.
    # No Request/opener is opened and no proxy authentication header is made.
    proxies = urllib.request.getproxies()
    configured = "https" in proxies
    bypassed = False
    selected = [("cache", CACHE_HOST)]
    if configured:
        scheme, user, password, hostport = urllib.request._parse_proxy(proxies["https"])
        bypassed = bool(urllib.request.proxy_bypass(CACHE_HOST))
        if not bypassed:
            parsed = urllib.parse.urlsplit("//" + urllib.parse.unquote(hostport))
            host = parsed.hostname
            require(host is not None and bool(host) and parsed.username is None and parsed.password is None
                    and not parsed.path and not parsed.query and not parsed.fragment,
                    "Invalid effective proxy authority")
            # Force validation of a configured port without publishing it.
            port = parsed.port
            require(port is None or 0 < port < 65536, "Invalid effective proxy port")
            selected.append(("proxy", host))
    selected.append(("github_control", CONTROL_HOST))
    require(2 <= len(selected) <= 3, "DNS role bound differs")
    return selected, {"proxy_configured": configured, "proxy_bypassed": bypassed}


def resolve_role(role, host):
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses:
            raise socket.gaierror(socket.EAI_NONAME, "No address result")
        return {"role": role, "status": "success"}
    except (OSError, UnicodeError, ValueError) as error:
        kind = ("gaierror" if type(error) is socket.gaierror else
                "TimeoutError" if isinstance(error, TimeoutError) else
                "OSError" if isinstance(error, OSError) else
                "UnicodeError" if isinstance(error, UnicodeError) else "ValueError")
        result = {"role": role, "status": "failure", "error_type": kind}
        number = getattr(error, "errno", None)
        if type(number) is int and -(2**31) <= number < 2**31:
            result["errno"] = number
        return result


def collect_dns():
    require("BAZEL_REMOTE_AUTH" not in os.environ, "Remote authentication must be absent")
    selected, flags = selected_hosts()
    return {"schema": 1, **flags,
            "results": [resolve_role(role, host) for role, host in selected]}


def validate_dns(detail):
    require(type(detail) is dict and set(detail) == {"schema", "proxy_configured", "proxy_bypassed", "results"}
            and type(detail["schema"]) is int and detail["schema"] == 1,
            "Invalid DNS detail shape")
    require(type(detail["proxy_configured"]) is bool and type(detail["proxy_bypassed"]) is bool
            and (not detail["proxy_bypassed"] or detail["proxy_configured"]), "Invalid proxy flags")
    roles = ["cache", "github_control"]
    if detail["proxy_configured"] and not detail["proxy_bypassed"]:
        roles.insert(1, "proxy")
    require(type(detail["results"]) is list and len(detail["results"]) == len(roles), "Invalid DNS role count")
    for role, row in zip(roles, detail["results"]):
        require(type(row) is dict and row.get("role") == role and row.get("status") in ("success", "failure"),
                "Invalid DNS role result")
        if row["status"] == "success":
            require(set(row) == {"role", "status"}, "Unexpected DNS success fields")
        else:
            require(set(row) in ({"role", "status", "error_type"}, {"role", "status", "error_type", "errno"})
                    and row["error_type"] in ERROR_TYPES, "Unexpected DNS failure fields")
            if "errno" in row:
                require(type(row["errno"]) is int and -(2**31) <= row["errno"] < 2**31, "Invalid DNS errno")
    return detail


def child_environment(smoke, inherited, directory):
    env = transport.child_environment(smoke, inherited, directory)
    env.pop("BAZEL_REMOTE_AUTH", None)
    return env


def private_root(parent, identity):
    require(parent.is_absolute() and parent.resolve() == parent and
            parent.is_relative_to(Path("/actions/_work")), "Different temporary root")
    for path in (parent, *parent.parents):
        require(stat.S_ISDIR(path.lstat().st_mode), "Temporary parent must not be a symlink")
    result = parent / ("cache-dns-%s-%s" % (identity["run"], identity["attempt"]))
    result.mkdir(mode=0o700)
    return result


def child_main(args, smoke):
    identity = smoke.check_identity(os.environ)
    expected = Path(os.environ["RUNNER_TEMP"]) / ("cache-dns-%s-%s" % (identity["run"], identity["attempt"]))
    require(args.detail_output == expected / "dns-detail.json" and not os.path.lexists(args.detail_output),
            "DNS child output path differs")
    require(expected.resolve() == expected and stat.S_ISDIR(expected.lstat().st_mode) and
            stat.S_IMODE(expected.lstat().st_mode) == 0o700, "DNS private directory differs")
    # Only fixed source constants, never authorization()/probe()/credentials().
    transport.write_fresh(args.detail_output, validate_dns(collect_dns()))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--detail-output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    smoke, credentials = transport.load_native(args.native_root)
    require(credentials.HOST == CACHE_HOST and credentials.ENDPOINT == "https://" + CACHE_HOST,
            "Fixed cache source endpoint differs")
    if args.child:
        try:
            require(args.output is None, "Unexpected child public output")
            return child_main(args, smoke)
        except BaseException:
            # urllib parse/DNS exceptions can contain the private proxy authority.
            # Preserve only the parent's fixed failure class; never print them.
            return 1
    require(args.output is not None and args.detail_output is None, "Invalid parent DNS arguments")
    identity = smoke.check_identity(os.environ)
    directory = private_root(Path(os.environ["RUNNER_TEMP"]), identity)
    require(args.output.parent == directory.parent and not os.path.lexists(args.output),
            "Public receipt must be fresh in runner temp")
    receipt = {"schema": 1, "status": "FAILED_DNS_DIAGNOSTIC", "scope": "DNS_ONLY_NOT_CACHE_QUALIFICATION",
               "identity": identity, "native_revision": transport.NATIVE, "cache_qualified": False,
               "HTTP_requests": 0, "credential_API_calls": 0, "bazel_invocations": 0}
    try:
        detail_path = directory / "dns-detail.json"
        argv = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                "--native-root", str(args.native_root), "--child", "--detail-output", str(detail_path)]
        # Reuse the exact reviewed child/group cleanup; private streams are never uploaded.
        result = smoke.run_command(directory, argv, child_environment(smoke, os.environ, directory),
            cwd=args.native_root, seconds=45, record=lambda record: None)
        require(result["returncode"] == 0 and result["reason"] is None and
                result["leader_reaped"] is True and result["group_closed"] is True and
                result["client_invocations"] == 1 and result["stdout_bytes"] == result["stderr_bytes"] == 0,
                "DNS child did not close cleanly")
        detail = transport.json_decoder().decode(transport.read_under(directory, detail_path.name, 4096).decode())
        receipt["dns"] = validate_dns(detail)
        receipt["status"] = "DNS_DIAGNOSTIC_CAPTURED_NOT_QUALIFIED"
        return 0
    except BaseException as error:
        receipt["error_type"] = type(error).__name__ if type(error).__name__ in (
            "DiagnosticFailure", "ValueError", "OSError", "TimeoutError", "FileNotFoundError", "InterruptedError") else "DiagnosticFailure"
        return 1
    finally:
        transport.write_fresh(args.output, receipt)


if __name__ == "__main__":
    raise SystemExit(main())
