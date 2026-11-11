"""Read fixed cache-URL candidates; no DNS, HTTP, credentials, Bazel or rc imports."""
import argparse
import ast
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[3]
DNS_PATH = ROOT / "tools/bazel/ci/vs_cache_dns.py"
with DNS_PATH.open("rb") as source:
    dns_bytes = source.read(65537)
if hashlib.sha256(dns_bytes).hexdigest() != "b6482f26f057d1d822792f12ccb330083ef4e0804e2845d5e4595dff3cb54809":
    raise ValueError("DNS adapter source binding differs")
_spec = importlib.util.spec_from_file_location("_vs_cache_configuration_dns", DNS_PATH)
dns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dns)
transport = dns.transport
require = transport.require

# These are inspected candidate names, not a claim that Bazel consumes them.
URL_ENV_NAMES = ("BAZEL_REMOTE_CACHE", "BAZEL_REMOTE_CACHE_URL", "BAZEL_CACHE_URL", "REMOTE_CACHE_URL")
RC_LIMIT = 65536
VALUE_LIMIT = 8192
MAX_RC_OPTIONS = 16
SOURCES = ("system_bazelrc", "runner_home_bazelrc")


def origin(value):
    if not value:
        return {"status": "disabled"}
    if len(value) > VALUE_LIMIT:
        return {"status": "value_too_large"}
    try:
        require(value.isascii() and all(32 < ord(c) < 127 for c in value), "Invalid URL characters")
        parsed = urllib.parse.urlsplit(value)
        require(parsed.scheme in ("http", "https", "grpc", "grpcs") and parsed.netloc,
                "Unsupported URL scheme")
        host, port = parsed.hostname, parsed.port
        require(host is not None and 0 < len(host) <= 253 and "%" not in host,
                "Invalid URL host")
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            require(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9.])?", host),
                    "Invalid URL host characters")
            host = host.lower()
        require(port is None or 0 < port < 65536, "Invalid URL port")
        # Reconstruct from only scheme/host/port: never userinfo, path, query or fragment.
        return {"status": "origin", "origin": parsed.scheme + "://" + host + (":" + str(port) if port else "")}
    except (ValueError, transport.DiagnosticFailure):
        return {"status": "invalid_value"}


def environment_candidates(inherited):
    return [{"env_name": name, **(origin(inherited[name]) if name in inherited else {"status": "absent"})}
            for name in URL_ENV_NAMES]


def rc_options(raw):
    require(len(raw) <= RC_LIMIT, "RC size bound exceeded")
    text = raw.decode("utf-8")
    require("\x00" not in text, "NUL in RC")
    options, buffer, imports, complex_lines = [], "", False, False
    lines = text.splitlines()
    require(len(lines) <= 2048, "RC line bound exceeded")
    for line in lines:
        buffer += line
        require(len(buffer) <= VALUE_LIMIT, "RC logical line bound exceeded")
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        tokens = shlex.split(buffer, comments=True, posix=True)
        buffer = ""
        if not tokens:
            continue
        if tokens[0] in ("import", "try-import"):
            imports = True
            continue
        # Inspect only complete standalone literal option lines. Do not guess which
        # token belongs to an unrelated header/helper flag or evaluate configurations.
        value = None
        command = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z0-9_.-]+)?", tokens[0]))
        if command and len(tokens) == 2 and tokens[1].startswith("--remote_cache="):
            value = tokens[1][len("--remote_cache="):]
        elif command and len(tokens) == 3 and tokens[1] == "--remote_cache":
            value = tokens[2]
        elif any("--remote_cache" in token for token in tokens):
            complex_lines = True
        if value is not None:
            require(len(options) < MAX_RC_OPTIONS, "RC option count exceeded")
            options.append(origin(value))
    require(not buffer, "Incomplete RC continuation")
    return {"options": options, "import_directives_ignored": imports,
            "complex_remote_cache_lines_not_interpreted": complex_lines}


def read_rc(category, path):
    require(category in SOURCES and path.is_absolute() and len(str(path)) <= 4096 and
            all(part not in (".", "..") for part in path.parts) and len(path.parts) <= 32,
            "Different RC scope")
    try:
        # Existing held-descriptor, no-follow, nonblocking regular-file reader.
        raw = transport.read_under(Path("/"), str(path)[1:], RC_LIMIT)
        return {"category": category, "status": "read", **rc_options(raw)}
    except FileNotFoundError:
        return {"category": category, "status": "absent"}
    except (OSError, ValueError, UnicodeError, transport.DiagnosticFailure):
        return {"category": category, "status": "refused"}



def endpoint_from_source(raw):
    require(len(raw) <= RC_LIMIT, "Runtime source size bound exceeded")
    tree = ast.parse(raw.decode("utf-8"))
    definitions = {"ENDPOINT": [], "HOST": []}
    stores = {name: sum(isinstance(node, ast.Name) and node.id == name and
                       isinstance(node.ctx, (ast.Store, ast.Del)) for node in ast.walk(tree))
              for name in definitions}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for name in definitions:
                if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                    definitions[name].append(node.value if len(node.targets) == 1 else None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in definitions:
            definitions[node.target.id].append(node.value)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name in stores:
                    stores[name] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in stores:
            stores[node.name] += 1
    if stores["ENDPOINT"] != 1 or len(definitions["ENDPOINT"]) != 1:
        return {"status": "uninterpreted"}
    endpoint = definitions["ENDPOINT"][0]
    if isinstance(endpoint, ast.Constant) and type(endpoint.value) is str:
        value = endpoint.value
    elif (isinstance(endpoint, ast.BinOp) and isinstance(endpoint.op, ast.Add) and
          isinstance(endpoint.left, ast.Constant) and endpoint.left.value == "https://" and
          isinstance(endpoint.right, ast.Name) and endpoint.right.id == "HOST" and
          stores["HOST"] == 1 and len(definitions["HOST"]) == 1 and
          isinstance(definitions["HOST"][0], ast.Constant) and type(definitions["HOST"][0].value) is str):
        # This one established source shape is not a general expression evaluator.
        value = "https://" + definitions["HOST"][0].value
    else:
        return {"status": "uninterpreted"}
    return {"status": "read", "endpoint": origin(value)}


def read_runtime_endpoint():
    row = {"category": "runtime_credentials_literal_endpoint"}
    try:
        raw = transport.read_under(Path("/"), "opt/sonic-runner/cache_credentials.py", RC_LIMIT)
        return {**row, **endpoint_from_source(raw)}
    except FileNotFoundError:
        return {**row, "status": "absent"}
    except (OSError, ValueError, UnicodeError, SyntaxError, transport.DiagnosticFailure):
        return {**row, "status": "refused"}


def collect_files():
    require("BAZEL_REMOTE_AUTH" not in os.environ, "Authentication must be absent")
    home = os.environ.get("HOME", "")
    require(home.startswith("/") and str(Path(home)) == home and ".." not in Path(home).parts,
            "Invalid runner HOME")
    return {"schema": 1, "rc_files": [read_rc(SOURCES[0], Path("/etc/bazel.bazelrc")),
                                     read_rc(SOURCES[1], Path(home) / ".bazelrc")],
            "runtime_endpoint": read_runtime_endpoint()}


def validate_candidate(row, provenance=None):
    require(type(row) is dict and row.get("status") in
            ("origin", "disabled", "value_too_large", "invalid_value", "absent"), "Invalid candidate")
    keys = {"status"} | ({provenance} if provenance else set())
    if row["status"] == "origin":
        keys.add("origin")
        require(type(row.get("origin")) is str and origin(row["origin"]) ==
                {"status": "origin", "origin": row["origin"]}, "Unsanitized origin")
    require(set(row) == keys, "Unexpected candidate fields")


def validate_files(detail):
    require(type(detail) is dict and set(detail) == {"schema", "rc_files", "runtime_endpoint"} and
            type(detail["schema"]) is int and detail["schema"] == 1 and
            type(detail["rc_files"]) is list and len(detail["rc_files"]) == 2, "Invalid file detail")
    for category, row in zip(SOURCES, detail["rc_files"]):
        require(type(row) is dict and row.get("category") == category and
                row.get("status") in ("read", "absent", "refused"), "Invalid file category")
        keys = {"category", "status"}
        if row["status"] == "read":
            keys |= {"options", "import_directives_ignored", "complex_remote_cache_lines_not_interpreted"}
            require(type(row["options"]) is list and len(row["options"]) <= MAX_RC_OPTIONS and
                    type(row["import_directives_ignored"]) is bool and
                    type(row["complex_remote_cache_lines_not_interpreted"]) is bool, "Invalid RC projection")
            for candidate in row["options"]:
                validate_candidate(candidate)
        require(set(row) == keys, "Unexpected RC fields")
    runtime = detail["runtime_endpoint"]
    require(type(runtime) is dict and runtime.get("category") == "runtime_credentials_literal_endpoint" and
            runtime.get("status") in ("read", "absent", "refused", "uninterpreted"), "Invalid runtime source projection")
    keys = {"category", "status"}
    if runtime["status"] == "read":
        keys.add("endpoint")
        validate_candidate(runtime["endpoint"])
    require(set(runtime) == keys, "Unexpected runtime source fields")
    return detail


def child_environment(smoke, inherited, directory):
    # Do not even look up BAZEL_REMOTE_AUTH or arbitrary environment values.
    names = ("HOME", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
             "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", *transport.IDENTITY_ENV)
    selected = {name: inherited[name] for name in names if name in inherited}
    return dns.child_environment(smoke, selected, directory)


def private_root(parent, identity):
    require(parent.is_absolute() and parent.resolve() == parent and
            parent.is_relative_to(Path("/actions/_work")), "Different temporary root")
    for path in (parent, *parent.parents):
        require(stat.S_ISDIR(path.lstat().st_mode), "Temporary parent must not be a symlink")
    result = parent / ("cache-configuration-%s-%s" % (identity["run"], identity["attempt"]))
    result.mkdir(mode=0o700)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--detail-output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    smoke, _credentials = transport.load_native(args.native_root)
    if args.child:
        try:
            identity = smoke.check_identity(os.environ)
            expected = Path(os.environ["RUNNER_TEMP"]) / ("cache-configuration-%s-%s" % (identity["run"], identity["attempt"]))
            require(args.output is None and args.detail_output == expected / "configuration-detail.json" and
                    not os.path.lexists(args.detail_output), "Different private output")
            require(expected.resolve() == expected and stat.S_ISDIR(expected.lstat().st_mode) and
                    stat.S_IMODE(expected.lstat().st_mode) == 0o700, "Different private directory")
            transport.write_fresh(args.detail_output, validate_files(collect_files()))
            return 0
        except BaseException:
            return 1
    require(args.output is not None and args.detail_output is None, "Invalid parent arguments")
    identity = smoke.check_identity(os.environ)
    directory = private_root(Path(os.environ["RUNNER_TEMP"]), identity)
    require(args.output.parent == directory.parent and not os.path.lexists(args.output), "Public output must be fresh")
    receipt = {"schema": 1, "status": "FAILED_CACHE_CONFIGURATION_DIAGNOSTIC", "identity": identity,
               "scope": "FIXED_CACHE_CONFIGURATION_CANDIDATES_ONLY", "native_revision": transport.NATIVE,
               "DNS_calls": 0, "HTTP_requests": 0, "credential_API_calls": 0, "bazel_invocations": 0,
               "cache_qualified": False, "effective_Bazel_configuration_determined": False}
    try:
        candidates = environment_candidates(os.environ)
        for name, row in zip(URL_ENV_NAMES, candidates):
            require(row["env_name"] == name, "Different environment category")
            validate_candidate(row, "env_name")
        receipt["environment"] = candidates
        detail_path = directory / "configuration-detail.json"
        argv = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "--native-root",
                str(args.native_root), "--child", "--detail-output", str(detail_path)]
        result = smoke.run_command(directory, argv, child_environment(smoke, os.environ, directory),
            cwd=args.native_root, seconds=45, record=lambda record: None)
        require(result["returncode"] == 0 and result["reason"] is None and
                result["leader_reaped"] is True and result["group_closed"] is True and
                result["client_invocations"] == 1 and result["stdout_bytes"] == result["stderr_bytes"] == 0,
                "Configuration child did not close cleanly")
        detail = transport.json_decoder().decode(transport.read_under(directory, detail_path.name, 32768).decode())
        receipt["configuration"] = validate_files(detail)
        receipt["status"] = "CACHE_CONFIGURATION_CAPTURED_NOT_QUALIFIED"
        return 0
    except BaseException as error:
        receipt["error_type"] = type(error).__name__ if type(error).__name__ in (
            "DiagnosticFailure", "ValueError", "OSError", "TimeoutError", "FileNotFoundError", "InterruptedError") else "DiagnosticFailure"
        return 1
    finally:
        transport.write_fresh(args.output, receipt)


if __name__ == "__main__":
    raise SystemExit(main())
