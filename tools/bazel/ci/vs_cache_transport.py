"""Observe one existing credential probe; never qualify a cache or run Bazel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
import urllib.error

NATIVE = "4bf648e812f28b05a2683c6cf7559470b0883d2a"
NATIVE_FILES = {
    "tools/bazel/ci/remote_cache_smoke.py": "679bda8f5a64ee00694968cc7f913ee694f792f0ad8094225a1da04b0be71237",
    "tools/bazel/ci/source_image_worker.py": "6c491e3daf8cdbdcc64257703742763040d986ed5d69a89cdec5e3366263b26d",
    "tools/bazel/ci/source_worker_preflight.py": "9ffde2a7a08e5cb86bf9d91a05c6b462537fe9586c0576ca912f6983e49d9e9f",
    "tools/bazel/cache/credentials.py": "9e44e83bccea514eaeea0a162d67295bb00d9be1cca764681b967380c7e0f5b0",
}
REASON_TYPES = frozenset(("gaierror", "SSLCertVerificationError", "SSLError", "TimeoutError",
    "ConnectionRefusedError", "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
    "PermissionError", "FileNotFoundError", "OSError", "str", "unclassified"))
IDENTITY_ENV = ("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR",
    "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "RUNNER_NAME", "GITHUB_WORKSPACE", "RUNNER_TEMP")

class DiagnosticFailure(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

def require(condition, reason):
    if not condition:
        raise DiagnosticFailure(reason)

def read_under(root, relative, cap):
    """Read one regular file through held, no-follow directory descriptors."""
    parts = Path(relative).parts
    require(parts and not Path(relative).is_absolute() and
            all(part not in (".", "..") for part in parts), "Invalid selected relative path")
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= cap,
                "Selected file type or size differs")
        raw = bytearray()
        while len(raw) <= cap:
            block = os.read(fd, min(65536, cap + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(len(raw) == before.st_size and fields(before) == fields(os.fstat(fd)) and
                fields(before) == fields(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)),
                "Selected file changed during observation")
        return bytes(raw)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)

def json_decoder():
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise DiagnosticFailure("Nonfinite JSON value")
    return json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)

def write_fresh(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")

def load_native(root):
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    require(root.is_absolute() and root.resolve() == root and root.is_relative_to(workspace.resolve()),
            "Native checkout is outside the workspace")
    for parent in (root, *root.parents):
        require(stat.S_ISDIR(parent.lstat().st_mode), "Native checkout parent is not a directory")
    for name, expected in NATIVE_FILES.items():
        require(hashlib.sha256(read_under(root, name, 512 * 1024)).hexdigest() == expected,
                "Native source binding differs")
    sys.path.insert(0, str(root))
    from tools.bazel.ci import remote_cache_smoke as smoke
    from tools.bazel.cache import credentials
    require(Path(smoke.__file__).resolve() == root / "tools/bazel/ci/remote_cache_smoke.py" and
            Path(credentials.__file__).resolve() == root / "tools/bazel/cache/credentials.py",
            "Imported probe module is outside the selected checkout")
    require(smoke.ROOT == root, "Imported native root differs")
    return smoke, credentials

def transport_reason(error):
    if not isinstance(error, urllib.error.URLError) or isinstance(error, urllib.error.HTTPError):
        return None
    reason = error.reason
    known = (socket.gaierror, ssl.SSLCertVerificationError, ssl.SSLError, TimeoutError,
             ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
             PermissionError, FileNotFoundError, OSError, str)
    kind = next((item.__name__ for item in known if type(reason) is item), "unclassified")
    result = {"reason_type": kind}
    if isinstance(reason, OSError) and type(reason.errno) is int and -(2**31) <= reason.errno < 2**31:
        result["errno"] = reason.errno
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = getattr(reason, "verify_code", None)
        if type(code) is int and 0 <= code < 2**31:
            result["ssl_verification_code"] = code
    return result

def call_existing_probe(credentials, output, detail):
    """One API invocation; its existing bounded retry/TLS/proxy policy is unchanged."""
    require(not os.path.lexists(output) and not os.path.lexists(detail), "Preserve existing probe evidence")
    write_fresh(detail.with_name("probe-api-intent.json"), {"api_invocations": 1, "helper_sha256": NATIVE_FILES["tools/bazel/cache/credentials.py"]})
    try:
        credentials.probe(output)
    except (ValueError, OSError, urllib.error.URLError) as error:
        write_fresh(detail, {"schema": 1, "probe_returncode": 1, "transport": transport_reason(error)})
        # Preserve the helper's fixed public CLI diagnostic, never exception text.
        print("Bazel remote cache access failed.", file=sys.stderr)
        return 1
    write_fresh(detail, {"schema": 1, "probe_returncode": 0, "transport": None})
    return 0

def validate_detail(detail, access, result):
    require(type(detail) is dict and set(detail) == {"schema", "probe_returncode", "transport"} and
            type(detail["schema"]) is int and detail["schema"] == 1 and
            type(detail["probe_returncode"]) is int and detail["probe_returncode"] in (0, 1),
            "Invalid diagnostic detail")
    require(result["returncode"] == detail["probe_returncode"] and result["reason"] is None and
            result["leader_reaped"] is True and result["group_closed"] is True and
            result["client_invocations"] == 1 and result["stdout_bytes"] == 0,
            "Diagnostic child did not close cleanly")
    require((detail["probe_returncode"] == 0) == (access["authenticated"] is True),
            "Probe status and receipt differ")
    transport = detail["transport"]
    if access.get("error_type") != "URLError":
        require(transport is None, "Unexpected transport detail")
        return None
    require(type(transport) is dict and "reason_type" in transport and
            set(transport) <= {"reason_type", "errno", "ssl_verification_code"} and
            transport["reason_type"] in REASON_TYPES, "Invalid transport detail")
    if "errno" in transport:
        require(type(transport["errno"]) is int and -(2**31) <= transport["errno"] < 2**31,
                "Invalid transport errno")
    if "ssl_verification_code" in transport:
        require(transport["reason_type"] == "SSLCertVerificationError" and
                type(transport["ssl_verification_code"]) is int and 0 <= transport["ssl_verification_code"] < 2**31,
                "Invalid SSL verification code")
    return dict(transport)

def child_environment(smoke, inherited, directory):
    env = smoke.version_environment(inherited, directory, "6.4.0")
    # The authentication, TLS and proxy values remain identical to the smoke probe.
    # No Bazel launcher is used and no Bazel-specific environment is needed.
    env.pop("USE_BAZEL_VERSION", None)
    env.pop("BAZELISK_HOME", None)
    env.update({key: inherited[key] for key in IDENTITY_ENV})
    return env

def private_root(parent, identity):
    require(parent.is_absolute() and parent.resolve() == parent and
            parent.is_relative_to(Path("/actions/_work")), "Different temporary root")
    for path in (parent, *parent.parents):
        require(stat.S_ISDIR(path.lstat().st_mode), "Temporary parent must not be a symlink")
    result = parent / ("cache-transport-%s-%s" % (identity["run"], identity["attempt"]))
    result.mkdir(mode=0o700)
    return result

def child_main(args, smoke, credentials):
    identity = smoke.check_identity(os.environ)
    expected = Path(os.environ["RUNNER_TEMP"]) / ("cache-transport-%s-%s" % (identity["run"], identity["attempt"]))
    require(args.probe_output == expected / "remote-cache-access.json" and
            args.detail_output == expected / "transport-detail.json" and
            not os.path.lexists(args.probe_output) and not os.path.lexists(args.detail_output),
            "Diagnostic child output path differs")
    require(expected.resolve() == expected and stat.S_ISDIR(expected.lstat().st_mode) and
            stat.S_IMODE(expected.lstat().st_mode) == 0o700, "Diagnostic private directory differs")
    smoke.worker.remote_cache_configuration(args.native_root)
    return call_existing_probe(credentials, args.probe_output, args.detail_output)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--detail-output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    smoke, credentials = load_native(args.native_root)
    if args.child:
        require(args.output is None, "Unexpected child public output")
        return child_main(args, smoke, credentials)
    require(args.output is not None and args.probe_output is None and args.detail_output is None,
            "Invalid parent diagnostic arguments")
    identity = smoke.check_identity(os.environ)
    directory = private_root(Path(os.environ["RUNNER_TEMP"]), identity)
    require(args.output.parent == directory.parent and not os.path.lexists(args.output),
            "Public receipt must be fresh in runner temp")
    receipt = {"status": "FAILED_DIAGNOSTIC", "scope": "CREDENTIAL_TRANSPORT_DIAGNOSTIC_ONLY",
               "identity": identity, "native_revision": NATIVE,
               "credential_helper_sha256": NATIVE_FILES["tools/bazel/cache/credentials.py"],
               "cache_qualified": False, "bazel_invocations": 0, "commands": []}
    try:
        receipt["stage"] = "credential_setup"
        configuration = smoke.worker.remote_cache_configuration(args.native_root)
        receipt["stage"] = "credential_probe"
        output = directory / "remote-cache-access.json"
        detail_path = directory / "transport-detail.json"
        argv = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                "--native-root", str(args.native_root), "--child",
                "--probe-output", str(output), "--detail-output", str(detail_path)]
        result = smoke.run_command(directory, argv, child_environment(smoke, os.environ, directory),
            cwd=args.native_root, seconds=45, record=lambda record: receipt["commands"].append(record))
        try:
            smoke.check_credential_probe_receipt(directory, output.name, configuration["endpoint"], result, receipt)
        except smoke.ProbeFailure as error:
            require(error.reason in ("Credential probe command failed", "Credential probe did not pass") and
                    "credential_probe" in receipt, "Invalid original helper receipt")
        original = read_under(directory, output.name, 4096)
        receipt["credential_probe_original"] = {"bytes": len(original), "sha256": hashlib.sha256(original).hexdigest()}
        detail = json_decoder().decode(read_under(directory, detail_path.name, 4096).decode())
        receipt["transport_detail"] = validate_detail(detail, receipt["credential_probe"], result)
        receipt["status"] = "CREDENTIAL_DIAGNOSTIC_CAPTURED_NOT_QUALIFIED"
        receipt["stage"] = "complete"
        return 0
    except BaseException as error:
        receipt["failure_reason"] = error.reason if isinstance(error, DiagnosticFailure) else "Runtime or I/O diagnostic failure"
        receipt["error_type"] = type(error).__name__ if type(error).__name__ in (
            "DiagnosticFailure", "ValueError", "OSError", "TimeoutError", "FileNotFoundError", "InterruptedError") else "DiagnosticFailure"
        return 1
    finally:
        receipt["at"] = smoke.now()
        write_fresh(args.output, receipt)

if __name__ == "__main__":
    raise SystemExit(main())
