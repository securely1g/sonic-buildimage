#!/usr/bin/env python3
"""Run a reviewed source-image driver on a private AMD64/Trixie worker."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.public_artifacts import safe_json, dependency_json, bep_summary
from tools.bazel.ci.source_worker_preflight import (
    POOLS, cpu_limit, measure, select_build_profile, worker_cache_root, worker_matches_pool)
from tools.bazel.gzip.source_archive import SOURCES
from tools.bazel.ci.image_worker_receipt import K3S_REMOTE_CACHE_POLICY, K3S_MANAGED_REMOTE_CACHE_POLICY
from tools.bazel.ci import native_apt_cache_policy

DRIVERS = {"dash-ha": "dash_ha", "sysmgr": "sysmgr", "orchagent": "orchagent", "sflow": "sflow", "otel": "otel", "syncd": "syncd", "gearbox": "gbsyncd", "dash-engine": "dash_engine", "gnmi": "gnmi", "gnmi-no-dialout": "gnmi", "framework": "mgmt_framework", "teamd": "teamd",
           "database": "database", "eventd": "eventd", "lldp": "lldp",
           "radv": "radv", "macsec": "macsec", "mux": "mux", "frr": "frr", "p4rt": "p4rt"}
WRAPPERS = {name: "libteam_draft_bazel.sh" for name in DRIVERS}
WRAPPERS["sysmgr"] = "review_bazel.sh"
WRAPPERS["otel"] = "review_bazel.sh"
WRAPPERS["syncd"] = "review_bazel.sh"
WRAPPERS["gearbox"] = "review_bazel.sh"
WRAPPERS["dash-engine"] = "review_bazel.sh"
WRAPPERS["gnmi"] = "review_bazel.sh"
WRAPPERS["gnmi-no-dialout"] = "review_bazel.sh"
WRAPPERS["framework"] = "review_bazel.sh"
WRAPPERS["p4rt"] = "review_bazel.sh"
for name in ("database", "eventd", "lldp", "radv", "macsec", "mux", "frr"):
    WRAPPERS[name] = "review_bazel.sh"
# Older service branches select their catalog in a dedicated image workflow.
# Keep the common workflow first for reviewed combined-source checkouts.
SOURCE_WORKFLOWS = {
    "eventd": "bazel-eventd-image.yml", "lldp": "bazel-lldp-image.yml",
    "radv": "bazel-radv-image.yml", "macsec": "bazel-macsec.yml",
    "mux": "bazel-mux-image.yml",
}
# Preserve the existing dedicated image workflows' fast Make/identity gates.
# These are fixed tests from the selected source, not additional owner suites.
BUILD_CHECKS = {
    "database": ("database_make_test.py",),
    "eventd": ("eventd_make_test.py",),
    "lldp": ("lldp_make_test.py", "lldp_identity_test.py"),
    "radv": ("radv_make_test.py",),
    "mux": ("mux_make_test.py",),
}
P4RT_OWNER = "e66b705bc9d87a86b555866914dc1909cbf3e092"
P4RT_OWNER_PATH = "src/sonic-p4rt/sonic-pins"
P4RT_OWNER_PROFILE = {"revision": P4RT_OWNER, "bazel": "6.4.0", "jobs": 1, "jvm_heap_gib": 2,
                      "scheduler_memory_mb": 4096, "cache_mode": "separate_persistent_worker_local"}
P4RT_PHASES = ("prepare-owner", "prepare-owner-runtime", "owner-tests", "build", "validate", "incremental", "loaded")
GNMI_PROFILES = {"gnmi": "enabled", "gnmi-no-dialout": "disabled"}
MIRROR_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/97cc502556b7ec3435d2d67b105315bcc18bb47e/ci/source_mirrors.cfg"
MIRROR_SHA = "ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720"


def mirror_configuration(original):
    require(hashlib.sha256(original).hexdigest() == MIRROR_SHA, "Source mirror configuration checksum mismatch")
    # BCR gawk@5.3.2 retains SHA256 f8c3486509de705192138b00ef2c00bbbdd0e84c30d5c07d23fc73a9dc4cc9cc.
    # The official Bazel mirror was independently verified to contain these exact bytes.
    destination = "https://mirror.bazel.build/ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz"
    expressions = [r"mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz",
                   r"ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz",
                   r"ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz"]
    lines = ["rewrite " + expression + " " + destination for expression in expressions]
    return original + ("\n# Exact verified mirrors; existing integrity checks remain enforced by Bazel.\n" +
                       "\n".join(lines) + "\n").encode()


def require(value, message):
    if not value:
        raise ValueError(message)


def selected_worker_pool():
    pool = os.environ.get("WORKER_POOL", "sonic-slave-container-dpool")
    require(pool in POOLS, "Unknown source-worker pool")
    return pool


def selection(image, revision):
    require(image in DRIVERS, "Unknown source image")
    if selected_worker_pool() == "sonic-slave-container-k3s":
        require(image in ("syncd", "gearbox", "p4rt"), "Image is not admitted to the K3s source-worker pool")
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Source revision must be a full lowercase Git SHA")
    catalog = os.environ.get("SOURCE_REGISTRY_REVISION", "")
    require(not catalog or re.fullmatch(r"[0-9a-f]{40}", catalog),
            "Registry revision must be a full lowercase Git SHA paired with the source")


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def output(argv, **kwargs):
    return subprocess.check_output(argv, text=True, **kwargs).strip()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def input_run_id():
    value = os.environ.get("SOURCE_INPUTS_RUN", "")
    require(not value or re.fullmatch(r"[1-9][0-9]{0,19}", value), "Source-input run must be a numeric GitHub run ID")
    return value


def verify_input_run(args):
    run_id = input_run_id()
    require(run_id and os.environ.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage", "Unexpected source-input repository")
    token = os.environ.get("GH_TOKEN", "")
    require(token, "Missing read-only Actions token")

    def api(suffix):
        request = urllib.request.Request("https://api.github.com/repos/securely1g/sonic-buildimage/" + suffix,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    selected = api("actions/runs/" + run_id)
    require(selected["repository"]["full_name"] == "securely1g/sonic-buildimage" and
            selected["path"] == ".github/workflows/bazel-oci.yml" and selected["name"] == "Bazel OCI" and
            re.fullmatch(r"[0-9a-f]{40}", selected["head_sha"]), "Unexpected source-download workflow")
    jobs = api("actions/runs/" + run_id + "/jobs?per_page=100")
    require(jobs["total_count"] <= 100, "Source-download job inventory requires another page")
    producer = [job for job in jobs["jobs"] if job["name"] == "OCI regression checks (AMD64)" and job["conclusion"] == "success"]
    require(len(producer) == 1 and all(any(step["name"] == name and step["conclusion"] == "success"
        for step in producer[0]["steps"]) for name in ("Retain the verified GitLab source downloads", "Upload the source downloads for the VS job")),
        "The source-download producer has not succeeded")
    artifacts = api("actions/runs/" + run_id + "/artifacts?per_page=100")
    require(artifacts["total_count"] <= 100, "Source artifact inventory requires another page")
    matches = [item for item in artifacts["artifacts"] if item["name"] == "bazel-repository-inputs"]
    require(len(matches) == 1 and not matches[0]["expired"], "Expected one available source-download artifact")
    artifact = matches[0]
    require(artifact["workflow_run"]["id"] == int(run_id) and artifact["workflow_run"]["head_sha"] == selected["head_sha"] and
            artifact["workflow_run"]["repository_id"] == selected["repository"]["id"],
            "Source artifact provenance differs")
    receipt = {"repository": "securely1g/sonic-buildimage", "run": int(run_id), "source_revision": selected["head_sha"],
               "producer_job": producer[0]["id"], "producer_status": "success", "artifact": artifact["id"],
               "artifact_name": artifact["name"], "artifact_bytes": artifact["size_in_bytes"]}
    (args.artifacts / "worker-input-run.json").write_text(json.dumps(receipt, indent=2) + "\n")


def check_source_inventory(archives, reused):
    expected = {name + "-" + version + "/src.tar.gz" for name, (version, _) in SOURCES.items()} | {"receipt.json"}
    if reused:
        # The legacy artifact also carries an unused Redis test helper. Never execute it here.
        expected |= {"test-helpers/redis-server", "test-helpers/redis-copyright", "test-helpers/redis.json"}
    actual = set()
    directories = {str(parent) for name in expected for parent in Path(name).parents if str(parent) != "."}
    require(archives.is_dir() and not archives.is_symlink(), "Missing source-download directory")
    for path in archives.rglob("*"):
        relative = str(path.relative_to(archives))
        require(not path.is_symlink() and path.resolve().is_relative_to(archives.resolve()), "Unsafe source-download path")
        if path.is_dir():
            require(relative in directories, "Unexpected source-download directory")
        else:
            require(path.is_file(), "Source download is not a regular file")
            actual.add(relative)
    require(actual == expected, "Source-download artifact has missing or extra files")
    receipt = json.loads((archives / "receipt.json").read_text())
    require(isinstance(receipt, list) and len(receipt) == len(SOURCES), "Incomplete source-download receipt")
    require({item["module"] for item in receipt} == set(SOURCES), "Source-download receipt repeats or omits a module")
    prefix = "artifacts/repository-inputs" if reused else "artifacts/source-image-inputs"
    for item in receipt:
        version, integrity = SOURCES[item["module"]]
        relative = item["module"] + "-" + version + "/src.tar.gz"
        require(item["version"] == version and item["integrity"] == integrity and item["bytes"] == (archives / relative).stat().st_size
                and item["archive"] == prefix + "/" + relative, "Source-download receipt differs from the pinned inputs")


def prepare_source_downloads(archives, cache):
    """Reuse the VS workflow's BCR-integrity verifier and persistent distdir."""
    check_source_inventory(archives, bool(input_run_id()))
    destination = cache / "distdir"
    receipt = json.loads(output([sys.executable, "-B", str(ROOT / "tools/bazel/gzip/source_archive.py"),
        "--archives-dir", str(archives), "--output", str(destination)]))
    directories = [destination / (name + "-" + version) for name, (version, _) in SOURCES.items()]
    return directories, [{key: item[key] for key in ("module", "version", "integrity", "bytes")} for item in receipt]


def source_check(workspace, revision):
    require(output(["git", "-C", str(workspace), "rev-parse", "HEAD"]) == revision,
            "Source checkout differs from the reviewed revision")
    run(["git", "-C", str(workspace), "diff", "--exit-code", "--ignore-submodules=none", "HEAD"])


def prepare_teamd_kernel(artifacts, modules=Path("/sys/module")):
    """Require the original Teamd fixture's preloaded modules without host changes."""
    required = ("team", "team_mode_loadbalance")
    missing = [name for name in required if not (modules / name).is_dir()]
    receipt = {"status": "failed" if missing else "passed", "read_only": True,
               "required_modules": list(required), "missing_modules": missing,
               "preloaded_modules": [name for name in required if name not in missing]}
    (artifacts / "worker-teamd-kernel.json").write_text(json.dumps(receipt, indent=2) + "\n")
    require(not missing, "Teamd worker requires preloaded kernel modules: " + ", ".join(missing))


def prepare_tap_fixture(artifacts):
    """Prove the original VS profile's TAP support before building its image."""
    device = Path("/dev/net/tun").lstat()
    require(stat.S_ISCHR(device.st_mode) and os.major(device.st_rdev) == 10 and os.minor(device.st_rdev) == 200,
            "Syncd requires the standard /dev/net/tun character device")
    parent_namespace = Path("/proc/self/ns/net").stat().st_ino
    script = '''import fcntl, json, os, struct, sys
namespace = os.stat("/proc/self/ns/net").st_ino
if namespace == int(sys.argv[1]):
    raise SystemExit("TAP probe is not in a private network namespace")
with open("/dev/net/tun", "r+b", buffering=0) as device:
    result = fcntl.ioctl(device.fileno(), 0x400454ca, struct.pack("16sH", b"sonic-probe%d", 0x0002 | 0x1000))
    interface = result[:16].split(b"\\0", 1)[0].decode()
    if not interface.startswith("sonic-probe"):
        raise SystemExit("Unexpected private TAP name")
# Closing this nonpersistent descriptor removes the TAP; unshare removes its namespace.
print(json.dumps({"private_network_namespace": True, "tap_created_and_closed": True}))
'''
    proof = json.loads(output(["sudo", "-n", "unshare", "--net", "--", sys.executable, "-I", "-c", script,
                               str(parent_namespace)], timeout=30))
    require(proof == {"private_network_namespace": True, "tap_created_and_closed": True},
            "Incomplete private TAP proof")
    (artifacts / "worker-tap.json").write_text(json.dumps({"status": "passed", **proof,
        "device_major": 10, "device_minor": 200, "scope": "disposable network namespace; no VM or KVM"}, indent=2) + "\n")


EXECUTION_NETWORK_PROBE = '''import json, os, urllib.error, urllib.request
names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
         "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
result = {"uid": os.getuid(), "proxy_names_present": {name: name in os.environ for name in names},
          "urllib_proxy_schemes": sorted(set(urllib.request.getproxies()) & {"http", "https", "ftp", "all", "no"})}
try:
    with urllib.request.urlopen("https://deb.debian.org/debian/dists/trixie/InRelease", timeout=10) as response:
        result["http_status"] = response.status
except urllib.error.HTTPError as error:
    result.update(http_status=error.code, error_class="HTTPError")
except Exception as error:
    result["error_class"] = type(error).__name__
print(json.dumps(result))
'''


APT_TRANSPORT_PROBE = '''import hashlib, json, os, pwd, resource, shlex, signal, stat, subprocess, tempfile, time
CAP = 1024 * 1024
URL = "https://deb.debian.org/debian/dists/trixie/InRelease"
PROXIES = tuple("Acquire::" + protocol + "::" + key for protocol in ("http", "https")
                for key in ("Proxy", "Proxy::deb.debian.org", "Proxy-Auto-Detect", "ProxyAutoDetect"))
FIELDS = PROXIES + ("Acquire::EnableSrvRecords/b", "APT::Sandbox::User", "Debug::NoDropPrivs/b")

def configuration():
    keys = FIELDS + tuple("Binary::" + method + "::" + key for method in ("http", "https") for key in FIELDS)
    args = ["apt-get", "shell"]
    for index, key in enumerate(keys):
        args.extend(("K" + str(index), key))
    # The executable is a query, not apt-get. argv[0] selects apt-get's Binary scope.
    raw = subprocess.run(args, executable="/usr/bin/apt-config", check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5).stdout
    if len(raw) > 65536:
        raise ValueError("configuration size")
    values = {}
    for token in shlex.split(raw):
        key, separator, value = token.partition("=")
        if not separator or key not in {"K" + str(i) for i in range(len(keys))} or key in values:
            raise ValueError("configuration format")
        values[key] = value
    for index in (8, 10):
        if "K" + str(index) in values and values["K" + str(index)] not in ("true", "false"):
            raise ValueError("configuration boolean")
    sandbox = values.get("K9", "_apt")
    return {"proxy_entries_present": ["K" + str(i) in values for i in range(8)],
            "srv_enabled": values.get("K8", "true") == "true", "srv_default": "K8" not in values,
            "sandbox_kind": sandbox if sandbox in ("_apt", "root", "") else "other",
            "no_drop_privs": values.get("K10", "false") == "true",
            "method_overrides_present": any("K" + str(i) in values for i in range(11, len(keys))),
            "apt_config_present": "APT_CONFIG" in os.environ}

def stop(process):
    # The leader is still unreaped: its PID/PGID cannot be recycled during cleanup.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(2)
    # A leader can exit before a method child. Always finish the owned group,
    # then reap the leader; waiting for the leader alone cannot prove cleanup.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)

def download(directory, disable_srv):
    path = os.path.join(directory, "srv-disabled" if disable_srv else "baseline")
    args = ["apt-get", "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=10",
            "-o", "Acquire::https::Timeout=10"]
    if disable_srv:
        args += ["-o", "Acquire::EnableSrvRecords=false"]
    args += ["download-file", URL, path]
    # apt-helper uses APT's normal HTTPS acquisition, with apt-get's Binary scope.
    process = subprocess.Popen(args, executable="/usr/lib/apt/apt-helper", start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {"status": "exit_failure"}
    leader_owned = True
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if state is not None:
                result["exit_code"] = state.si_status if state.si_code == os.CLD_EXITED else -state.si_status
                break
            time.sleep(0.05)
        else:
            result["status"] = "timeout"
    except ChildProcessError:
        # Do not signal a numeric group if an unexpected external reaper took
        # away the leader identity that protects it against PID reuse.
        leader_owned = False
        raise
    finally:
        if leader_owned:
            stop(process)
    if result.get("exit_code") == 0:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or not 0 < size <= CAP:
                return {"status": "size_or_type_rejected", "exit_code": 0}
            data = stream.read(CAP + 1)
        if len(data) != size:
            return {"status": "size_or_type_rejected", "exit_code": 0}
        result.update(status="downloaded", bytes=size, sha256=hashlib.sha256(data).hexdigest())
    return result

def main():
    result = {"scope": "transport_only_no_signature_verification"}
    try:
        if os.getuid() != 0:
            raise PermissionError("diagnostic identity")
        config = configuration()
        result["config"] = config
        if config["method_overrides_present"] or config["sandbox_kind"] == "other":
            result["status"] = "unsupported_configuration"
            return result
        account = pwd.getpwnam("_apt") if config["sandbox_kind"] == "_apt" else None
        if account is not None and account.pw_uid == 0:
            raise ValueError("sandbox identity")
        # Generic download-file has no MaxReleaseFileSize bound. This limit is
        # inherited by the helper and its methods, and applies only in this child.
        resource.setrlimit(resource.RLIMIT_FSIZE, (CAP, CAP))
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix="sonic-apt-diagnostic-") as directory:
            if account is not None:
                os.chown(directory, account.pw_uid, account.pw_gid)
            # Private mode0700 remains accessible to the configured sandbox user;
            # an inaccessible root-only target would make APT fall back to root.
            result["attempts"] = []
            for disable_srv in (False, True):
                try:
                    result["attempts"].append(download(directory, disable_srv))
                except Exception as error:
                    result["attempts"].append({"status": "error", "error_class": type(error).__name__})
        result["status"] = "completed"
    except Exception as error:
        result.update(status="error", error_class=type(error).__name__)
    return result

if __name__ == "__main__":
    print(json.dumps(main()))
'''


def prepare_apt_transport(artifacts):
    """Keep only typed, bounded transport evidence; never publish APT output."""
    try:
        value = json.loads(output(["sudo", "-n", sys.executable, "-I", "-c", APT_TRANSPORT_PROBE],
                                  timeout=50, stderr=subprocess.DEVNULL))
        require(set(value) <= {"scope", "status", "config", "attempts", "error_class"} and
                value["scope"] == "transport_only_no_signature_verification" and
                value["status"] in {"completed", "error", "unsupported_configuration"}, "Unexpected APT diagnostic")
        if value["status"] == "completed":
            require("config" in value and "attempts" in value, "Incomplete APT diagnostic")
        if value["status"] == "unsupported_configuration":
            require("config" in value and "attempts" not in value, "Unexpected skipped APT attempts")
        if "config" in value:
            config = value["config"]
            booleans = {"srv_enabled", "srv_default", "no_drop_privs", "method_overrides_present", "apt_config_present"}
            require(set(config) == booleans | {"proxy_entries_present", "sandbox_kind"} and
                    all(type(config[key]) is bool for key in booleans) and
                    type(config["proxy_entries_present"]) is list and len(config["proxy_entries_present"]) == 8 and
                    all(type(item) is bool for item in config["proxy_entries_present"]) and
                    config["sandbox_kind"] in {"_apt", "root", "", "other"}, "Unexpected APT configuration diagnostic")
        if "attempts" in value:
            require(type(value["attempts"]) is list and len(value["attempts"]) == 2, "Unexpected APT attempts")
        for row in [value, *value.get("attempts", [])]:
            if row is not value:
                require(set(row) <= {"status", "exit_code", "bytes", "sha256", "error_class"} and
                        row["status"] in {"downloaded", "exit_failure", "timeout", "size_or_type_rejected", "error"},
                        "Unexpected APT transport result")
                if "exit_code" in row:
                    require(type(row["exit_code"]) is int and -128 <= row["exit_code"] <= 255, "Unexpected APT exit")
                if row["status"] == "downloaded":
                    require(row.get("exit_code") == 0 and type(row.get("bytes")) is int and
                            0 < row["bytes"] <= 1024 * 1024 and type(row.get("sha256")) is str and
                            re.fullmatch("[0-9a-f]{64}", row["sha256"]), "Unexpected APT downloaded bytes")
                else:
                    require("bytes" not in row and "sha256" not in row, "Unexpected partial APT digest")
            if "error_class" in row:
                require(type(row["error_class"]) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", row["error_class"]),
                        "Unexpected APT error class")
        safe_json(value)
    except Exception as error:
        value = {"status": "error", "error_class": type(error).__name__}
    try:
        (artifacts / "worker-apt-transport.json").write_text(json.dumps(value, indent=2) + "\n")
    except OSError:
        print("Worker APT transport diagnostic could not be retained", file=sys.stderr)


def prepare_execution_network(artifacts):
    """Compare the two existing APT execution identities without recording values."""
    result = {}
    names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    for context, prefix, expected_uid in (("worker", [], 1000), ("sudo", ["sudo", "-n"], 0)):
        try:
            value = json.loads(output([*prefix, sys.executable, "-I", "-c", EXECUTION_NETWORK_PROBE],
                                      timeout=20, stderr=subprocess.DEVNULL))
            require(set(value) <= {"uid", "proxy_names_present", "urllib_proxy_schemes", "http_status", "error_class"},
                    "Unexpected network diagnostic field")
            require(type(value["uid"]) is int and value["uid"] == expected_uid, "Unexpected diagnostic UID")
            require(set(value["proxy_names_present"]) == names and
                    all(type(present) is bool for present in value["proxy_names_present"].values()),
                    "Unexpected proxy presence diagnostic")
            require(isinstance(value["urllib_proxy_schemes"], list) and
                    all(scheme in {"http", "https", "ftp", "all", "no"} for scheme in value["urllib_proxy_schemes"]),
                    "Unexpected proxy scheme diagnostic")
            require("http_status" in value or "error_class" in value, "Missing network diagnostic outcome")
            if "http_status" in value:
                require(type(value["http_status"]) is int and 100 <= value["http_status"] <= 599,
                        "Unexpected HTTP status diagnostic")
            if "error_class" in value:
                require(isinstance(value["error_class"], str) and
                        re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value["error_class"]),
                        "Unexpected network error class")
            safe_json(value)
            result[context] = value
        except Exception as error:
            result[context] = {"error_class": type(error).__name__}
    try:
        (artifacts / "worker-execution-network.json").write_text(json.dumps(result, indent=2) + "\n")
    except OSError:
        # The existing APT command remains authoritative even if diagnostics cannot be retained.
        print("Worker execution network diagnostic could not be retained", file=sys.stderr)
    prepare_apt_transport(artifacts)


APT_OWNED_CACHE_TRANSPORT = '''import errno, json, os, socket, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
from tools.bazel.ci import native_apt_cache_policy as policy


def select_transport():
    packages = json.loads(sys.argv[2])
    policy.require(type(packages) is list and packages and packages == sorted(set(packages))
                   and set(packages) <= {"libarchive-tools", "gdb", "binutils"})
    proof = policy.validate_report(policy.inspect())
    result = {"schema": 1, "status": "not_selected", "policy": proof, "completed_actions": []}
    if proof["status"] != "eligible":
        return result
    # The installed endpoint must match the exact reviewed owner before this
    # bounded numeric-IP connection. There is no HTTP request or alternate URL.
    address = policy.verified_cache_address()
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(3)
    try:
        connection.connect(address)
    except OSError as error:
        if isinstance(error, TimeoutError) or error.errno == errno.ETIMEDOUT:
            result["cache_connection"] = "timeout"
        else:
            result["cache_connection"] = {errno.ECONNREFUSED: "refused", errno.ENETUNREACH: "network_unreachable",
                                           errno.EHOSTUNREACH: "host_unreachable"}.get(error.errno, "other_error")
    else:
        result["cache_connection"] = "reachable"
    finally:
        connection.close()
    if result["cache_connection"] in {"reachable", "other_error"}:
        return result
    commands = [("update", ["-o", "APT::Update::Error-Mode=any", "update"]),
                ("install", ["install", "-y", "--no-install-recommends", *packages])]
    try:
        # Only this invocation uses fresh Debian indexes and package downloads.
        # Keep the system's installed-package database and signing trust intact.
        with tempfile.TemporaryDirectory(prefix="sonic-native-tools-", dir="/tmp") as directory:
            os.chmod(directory, 0o755)
            for name in ("sources", "lists", "archives"):
                os.mkdir(os.path.join(directory, name), 0o755)
                os.chmod(os.path.join(directory, name), 0o755)
            options = ["-o", "Acquire::EnableSrvRecords=false", "-o", "Acquire::Retries=0",
                       "-o", "Acquire::http::Timeout=10", "-o", "Acquire::https::Timeout=10",
                       *policy.DIRECT_OPTIONS,
                       "-o", "Dir::Etc::sourcelist=" + str(policy.SOURCE),
                       "-o", "Dir::Etc::sourceparts=" + os.path.join(directory, "sources"),
                       "-o", "Dir::State::lists=" + os.path.join(directory, "lists"),
                       "-o", "Dir::Cache::archives=" + os.path.join(directory, "archives"),
                       "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache="]
            for action, arguments in commands:
                policy.require(policy.validate_report(policy.inspect()) == proof)
                policy.require(not os.listdir(os.path.join(directory, "sources")))
                # Bound the ordinary APT process group without changing host policy.
                subprocess.run(["/usr/bin/timeout", "--signal=TERM", "--kill-after=10s", "300s",
                                "/usr/bin/apt-get", *options, *arguments], check=True, stdin=subprocess.DEVNULL,
                               stdout=sys.stderr, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
                result["completed_actions"].append(action)
            result["status"] = "direct_success"
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result["status"] = "direct_failed"
        result["error_class"] = type(error).__name__
        if isinstance(error, subprocess.CalledProcessError):
            result["exit_code"] = error.returncode
    return result


if __name__ == "__main__":
    print(json.dumps(select_transport(), sort_keys=True))
'''


def select_tools_cache_transport(missing, artifacts):
    if selected_worker_pool() != "sonic-slave-container-k3s" or not native_apt_cache_policy.proxy_environment_absent(os.environ):
        return False
    raw = output(["sudo", "-n", sys.executable, "-I", "-S", "-B", "-c", APT_OWNED_CACHE_TRANSPORT,
                  str(ROOT), json.dumps(missing)], timeout=660)
    require(len(raw) <= 16384, "Oversized optional APT transport report")
    value = json.loads(raw)
    require(type(value) is dict and set(value) <= {"schema", "status", "policy", "completed_actions",
            "cache_connection", "error_class", "exit_code"} and value["schema"] == 1 and
            value["status"] in {"not_selected", "direct_success", "direct_failed"}, "Invalid optional APT transport report")
    native_apt_cache_policy.validate_report(value["policy"])
    require(value["completed_actions"] in ([], ["update"], ["update", "install"]), "Invalid optional APT transport actions")
    if "cache_connection" in value:
        require(value["cache_connection"] in {"timeout", "refused", "network_unreachable", "host_unreachable",
                                              "reachable", "other_error"}, "Invalid optional APT connection result")
    if value["status"] == "not_selected":
        require(value["completed_actions"] == [] and (
                (value["policy"]["status"] == "not_eligible" and "cache_connection" not in value) or
                (value["policy"]["status"] == "eligible" and value.get("cache_connection") in {"reachable", "other_error"})),
                "Ambiguous optional APT transport selection")
    if value["status"] in {"direct_success", "direct_failed"}:
        require(value["policy"]["status"] == "eligible" and value.get("cache_connection") in
                {"timeout", "refused", "network_unreachable", "host_unreachable"}, "Invalid optional APT transport admission")
    if value["status"] == "direct_success":
        require(value["completed_actions"] == ["update", "install"] and "error_class" not in value and
                "exit_code" not in value, "Incomplete optional APT transport")
    if "error_class" in value:
        require(type(value["error_class"]) is str and re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,63}", value["error_class"]),
                "Invalid optional APT transport error")
    if "exit_code" in value:
        require(type(value["exit_code"]) is int and -128 <= value["exit_code"] <= 255 and value["exit_code"] != 0,
                "Invalid optional APT transport exit")
    safe_json(value)
    (artifacts / "worker-apt-transport-selection.json").write_text(json.dumps(value, indent=2) + "\n")
    if value["status"] == "direct_failed":
        if "exit_code" in value:
            action = "install" if value["completed_actions"] else "update"
            raise subprocess.CalledProcessError(value["exit_code"], ["apt-get", *native_apt_cache_policy.DIRECT_OPTIONS, action])
        raise RuntimeError("Owned optional APT transport failed before completion")
    return value["status"] == "direct_success"

def prepare_validation_tools(artifacts):
    """Install only missing execution tools in the disposable slave container."""
    packages = {"bsdtar": "libarchive-tools", "gdb": "gdb", "readelf": "binutils", "objcopy": "binutils"}
    missing = sorted({package for tool, package in packages.items() if shutil.which(tool) is None})
    if missing:
        try:
            if not select_tools_cache_transport(missing, artifacts):
                run(["sudo", "-n", "apt-get", "-o", "Acquire::EnableSrvRecords=false", "update"])
                run(["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                     "-o", "Acquire::EnableSrvRecords=false", "install", "-y",
                     "--no-install-recommends", *missing])
        except subprocess.CalledProcessError:
            try:
                prepare_execution_network(artifacts)
            except Exception:
                # Diagnostics must not replace the original APT failure.
                print("Worker execution network diagnostic failed", file=sys.stderr)
            raise
    for tool in packages:
        require(shutil.which(tool), "Missing image validation tool: " + tool)
    versions = output(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n", *sorted(set(packages.values()))])
    (artifacts / "worker-execution-tools.json").write_text(json.dumps(
        {"packages_installed": missing, "package_versions": versions.splitlines(),
         "scope": "disposable_worker_execution_tools"}, indent=2) + "\n")


def catalog_for(image, workspace, registry_revision=""):
    require(not registry_revision or re.fullmatch(r"[0-9a-f]{40}", registry_revision),
            "Registry revision must be a full lowercase Git SHA")
    workflow = (workspace / ".github/workflows/bazel-oci.yml").read_text()
    catalogs = sorted(set(re.findall(r"^ +SONIC_BAZEL_REGISTRY_REVISION: ([0-9a-f]{40})$", workflow, re.M)))
    wrapper = WRAPPERS[image]
    wrapper_pattern = r"^ +BAZEL: (?:\$\{\{ github.workspace \}\}/|\./)?tools/bazel/ci/" + re.escape(wrapper) + r"$"
    selected_wrapper = re.search(wrapper_pattern, workflow, re.M)
    if wrapper == "review_bazel.sh" and image in SOURCE_WORKFLOWS and not (selected_wrapper and len(catalogs) == 1):
        workflow = (workspace / ".github/workflows" / SOURCE_WORKFLOWS[image]).read_text()
        selected_wrapper = re.search(wrapper_pattern, workflow, re.M)
        if image == "macsec" and not selected_wrapper:
            # This workflow calls its fixed driver without --bazel; bind that
            # driver's declared default rather than an unrelated OCI job.
            driver = (workspace / "tools/bazel/ci/macsec_image.py").read_text()
            defaults = [keyword.value.value for call in ast.walk(ast.parse(driver))
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "add_argument" and call.args
                        and isinstance(call.args[0], ast.Constant) and call.args[0].value == "--bazel"
                        for keyword in call.keywords if keyword.arg == "default"
                        and isinstance(keyword.value, ast.Constant)]
            selected_wrapper = (re.search(r"^ +python3 -B tools/bazel/ci/macsec_image\.py build --artifacts artifacts/macsec$", workflow, re.M)
                                and defaults == ["tools/bazel/ci/review_bazel.sh"])
        values = re.findall(r"^ +SONIC_BAZEL_REGISTRY_REVISION: (.+)$", workflow, re.M)
        catalogs = []
        for value in values:
            match = re.fullmatch(r"([0-9a-f]{40})|\$\{\{ inputs\.registry_revision \|\| '([0-9a-f]{40})' \}\}", value)
            require(match, "Selected image workflow has an unsupported registry selection")
            catalogs.append(match.group(1) or match.group(2))
        catalogs = sorted(set(catalogs))
    require(selected_wrapper, "Selected workflow uses another Bazel entrypoint")
    if wrapper != "review_bazel.sh":
        text = (workspace / "tools/bazel/ci" / wrapper).read_text()
        selected = re.findall(r"^export SONIC_BAZEL_REGISTRY_REVISION=([0-9a-f]{40})$", text, re.M)
        require(len(selected) == 1 and (not catalogs or selected == catalogs),
                "Workflow and compatibility entrypoint catalogs differ")
        require(not registry_revision or registry_revision == selected[0],
                "Compatibility entrypoint cannot override its pinned catalog")
        return selected[0]
    require(len(catalogs) == 1, "Expected one immutable global workflow catalog")
    return registry_revision or catalogs[0]


def memory_snapshot():
    root = Path("/sys/fs/cgroup")
    result = {name: int((root / ("memory." + name)).read_text()) for name in ("current", "peak")}
    result["limit"] = (root / "memory.max").read_text().strip()
    result["swap_max"] = (root / "memory.swap.max").read_text().strip()
    result["swap_current"] = int((root / "memory.swap.current").read_text())
    result["cpu_max"] = (root / "cpu.max").read_text().strip()
    result["events"] = {name: int(value) for name, value in
                        (line.split() for line in (root / "memory.events").read_text().splitlines())}
    statistics = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
    result["usage"] = {name: int(statistics[name]) for name in ("anon", "file", "kernel")}
    return result


def worker_work_directory(pool, runner):
    directory = worker_cache_root(pool) / "source-images" / runner
    if pool == "sonic-slave-container-k3s":
        require(worker_matches_pool(runner, pool), "Unexpected K3s worker identity")
        identity = [os.environ.get(name, "") for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")]
        require(all(re.fullmatch(r"[1-9][0-9]*", value) for value in identity),
                "K3s working state requires a run and attempt")
        directory /= "run-" + "-".join(identity)
    return directory


def prepare_work_directory(pool, runner, image):
    directory = worker_work_directory(pool, runner)
    require(directory.resolve() == directory, "Cache path must not traverse symlinks")
    if pool == "sonic-slave-container-k3s":
        # Preflight proved this worker's parent on /actions. Never adopt old state.
        require(directory.parent.is_dir(), "K3s working-state parent was not prepared")
        directory.mkdir(mode=0o700)
    names = ("repository", "bazelisk", image + "/output-base")
    if pool != "sonic-slave-container-k3s":
        names = ("repository", "disk", "bazelisk", image + "/output-base")
    for name in names:
        (directory / name).mkdir(parents=True, exist_ok=True)
    return directory


def managed_cache_source(helper, expected_sha):
    """Read the exact reviewed source through a held, bounded regular-file FD."""
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
    fd = os.open(helper, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022 or
                before.st_size != 7646 or helper.resolve(strict=True) != helper or
                not os.access(helper, os.X_OK)):
            raise ValueError("Managed remote-cache helper differs from the reviewed installed owner")
        source = os.read(fd, 7647)
        after = os.fstat(fd)
        current = helper.lstat()
        identity = tuple(getattr(before, field) for field in fields)
        if (identity != tuple(getattr(after, field) for field in fields) or
                identity != tuple(getattr(current, field) for field in fields) or
                helper.resolve(strict=True) != helper or len(source) != 7646 or
                hashlib.sha256(source).hexdigest() != expected_sha):
            raise ValueError("Managed remote-cache helper differs from the reviewed installed owner")
        return source
    finally:
        os.close(fd)


def managed_cache_helper():
    selected = Path(K3S_MANAGED_REMOTE_CACHE_POLICY["credential_helper_path"])
    try:
        root = selected.parent
        original = selected.lstat()
        root_info = root.lstat()
        helper = selected.resolve(strict=True)
        require(stat.S_ISDIR(root_info.st_mode) and root_info.st_uid == 0 and
                root.resolve(strict=True) == root and
                (not root_info.st_mode & 0o022 or os.statvfs(root).f_flag & os.ST_RDONLY),
                "Managed remote-cache helper differs from the reviewed installed owner: runtime directory")
        require(helper.name == selected.name and
                (helper == selected or (stat.S_ISLNK(original.st_mode) and original.st_uid == 0 and
                 helper.parent.parent == root)),
                "Managed remote-cache helper differs from the reviewed installed owner: projected selection")
        directory = helper.parent.lstat()
        require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == 0 and
                helper.parent.resolve(strict=True) == helper.parent and
                (not directory.st_mode & 0o022 or os.statvfs(helper.parent).f_flag & os.ST_RDONLY),
                "Managed remote-cache helper differs from the reviewed installed owner")
        managed_cache_source(helper, K3S_MANAGED_REMOTE_CACHE_POLICY["credential_helper_sha256"])
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        for path, before in ((selected, original), (root, root_info), (helper.parent, directory)):
            after = path.lstat()
            require(all(getattr(before, field) == getattr(after, field) for field in fields),
                    "Managed remote-cache helper selection changed")
        require(selected.resolve(strict=True) == helper, "Managed remote-cache helper selection changed")
    except (OSError, RuntimeError):
        raise ValueError("Managed remote-cache helper unavailable; legacy fallback is disabled") from None
    return helper


def load_managed_cache_owner(helper):
    # Compile the held, verified bytes with the stable owner identity used by diagnostics.
    import types
    source = managed_cache_source(helper, K3S_MANAGED_REMOTE_CACHE_POLICY["credential_helper_sha256"])
    owner = types.ModuleType("reviewed_native_cache_owner")
    owner.__file__ = K3S_MANAGED_REMOTE_CACHE_POLICY["credential_helper_path"]
    exec(compile(source, owner.__file__, "exec"), owner.__dict__)
    return owner


def remote_cache_configuration(workspace):
    # The installed owner selects public settings; this does not perform HTTP.
    helper = managed_cache_helper()
    policy = K3S_MANAGED_REMOTE_CACHE_POLICY
    require(os.getuid() == os.geteuid() == 1000, "Managed cache settings require the worker UID")
    owner = load_managed_cache_owner(helper)
    try:
        require(owner.HOST_CREDENTIALS.is_file(), "Host-managed cache configuration is required")
        selected = owner.settings()
        require(owner.HOST_CREDENTIALS.is_file(), "Host-managed cache configuration disappeared")
    except (OSError, ValueError):
        raise ValueError("Managed cache configuration unavailable; legacy fallback is disabled") from None
    require(type(selected) is dict and set(selected) == {"endpoint", "host"}, "Invalid owner public settings")
    return {"endpoint": selected["endpoint"], "host": selected["host"], "helper": str(helper),
            "helper_sha256": policy["credential_helper_sha256"], "provider": policy["provider"]}


def managed_cache_owner_program():
    # The installed owner alone parses credentials and performs the HTTPS probe.
    # Its source identity is checked again inside this isolated bounded child.
    import inspect
    return ("import hashlib, json, os, stat, sys, types\nfrom pathlib import Path\n" +
            inspect.getsource(managed_cache_source) + r"""
os.umask(0o077)
os.environ.pop('BAZEL_REMOTE_AUTH', None)
configuration = json.loads(sys.argv[1])
output = Path(sys.argv[2])
value = {'schema': 1, 'provider': configuration['provider'],
         'endpoint': configuration['endpoint'], 'host': configuration['host'],
         'helper_sha256': configuration['helper_sha256'], 'access_probe': 'failed',
         'stage': 'owner_identity'}
try:
    helper = Path(configuration['helper'])
    if os.getuid() != 1000 or os.geteuid() != 1000:
        raise ValueError('Owner identity differs')
    source = managed_cache_source(helper, configuration['helper_sha256'])
    owner = types.ModuleType('reviewed_native_cache_owner')
    owner.__file__ = str(helper)
    exec(compile(source, str(helper), 'exec'), owner.__dict__)
    value['stage'] = 'host_configuration'
    if not owner.HOST_CREDENTIALS.is_file():
        raise ValueError('Host configuration required')
    expected = {'endpoint': configuration['endpoint'], 'host': configuration['host']}
    value['stage'] = 'settings'
    if owner.settings() != expected:
        raise ValueError('Public settings differ')
    value['stage'] = 'access'
    owner.probe_access()
    value['stage'] = 'stable_settings'
    if not owner.HOST_CREDENTIALS.is_file() or owner.settings() != expected:
        raise ValueError('Host configuration or public settings changed')
    value.update(stage='complete', access_probe='passed')
except BaseException as error:
    kind = type(error).__name__
    value['error_type'] = kind if kind in ('ValueError', 'OSError', 'FileNotFoundError',
        'PermissionError', 'TimeoutError', 'ImportError') else 'OwnerFailure'
with output.open('x') as stream:
    json.dump(value, stream, sort_keys=True)
    stream.write('\n')
raise SystemExit(0 if value['access_probe'] == 'passed' else 1)
""")


def managed_cache_probe_command(configuration, receipt):
    remote_cache_options(configuration)
    require(configuration.get("provider") == "host_managed_native_runtime", "Managed cache owner required")
    return [sys.executable, "-I", "-S", "-B", "-c", managed_cache_owner_program(),
            json.dumps(configuration, sort_keys=True), str(receipt)]


def validate_managed_cache_probe(value, configuration):
    require(type(value) is dict and type(value.get("schema")) is int and value == {"schema": 1, "provider": configuration["provider"],
            "endpoint": configuration["endpoint"], "host": configuration["host"],
            "helper_sha256": configuration["helper_sha256"], "access_probe": "passed", "stage": "complete"},
            "Host-managed K3s remote-cache access probe did not pass")


def prepare_remote_cache(workspace, directory):
    configuration = remote_cache_configuration(workspace)
    receipt = directory / "remote-cache-access.json"
    require(not os.path.lexists(receipt), "Remote-cache probe output must be fresh")
    try:
        run(managed_cache_probe_command(configuration, receipt),
            timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Host-managed K3s remote-cache access failed; no local-cache fallback") from None
    require(receipt.stat().st_size <= 4096, "Managed cache probe receipt exceeds bound")
    validate_managed_cache_probe(json.loads(receipt.read_text()), configuration)
    return configuration


def remote_cache_options(configuration):
    if configuration.get("provider") == "host_managed_native_runtime":
        require(configuration == remote_cache_configuration(None), "Unexpected managed remote-cache configuration")
        return ["--remote_cache=" + configuration["endpoint"],
                "--credential_helper=" + configuration["host"] + "=" + configuration["helper"],
                "--disk_cache=", "--repository_cache=", "--remote_executor="]
    # Preserve the historical schema2 renderer; new K3s preparation never selects it.
    require(set(configuration) == {"endpoint", "helper", "helper_sha256"} and
            configuration["endpoint"] == K3S_REMOTE_CACHE_POLICY["endpoint"] and
            configuration["helper_sha256"] == K3S_REMOTE_CACHE_POLICY["credential_helper_sha256"] and
            Path(configuration["helper"]).is_absolute(), "Unexpected remote-cache configuration")
    host = K3S_REMOTE_CACHE_POLICY["endpoint"].removeprefix("https://")
    return ["--remote_cache=" + configuration["endpoint"],
            "--credential_helper=" + host + "=" + configuration["helper"], "--disk_cache="]



def owner_build_profile(pool):
    profile = dict(P4RT_OWNER_PROFILE)
    if pool == "sonic-slave-container-k3s":
        profile["cache_mode"] = "separate_remote_only_run_attempt"
    return profile


def launcher_text(binary, base, profile, remote_cache=None):
    flags = (remote_cache_options(remote_cache) if remote_cache is not None else ["--remote_cache="]) + ["--remote_download_regex=(^|.*/)tools/build_tools/kernel_runtime_(amd64|arm64)(/.*)?$",
             "--jobs=" + str(profile["jobs"]), "--local_resources=cpu=" + str(profile["cpu"]),
             "--local_resources=memory=" + str(profile["scheduler_memory_mb"])]
    return ("#!/usr/bin/env python3\nimport os, sys\n" +
            "binary = " + repr(binary) + "\nbase = " + repr(str(base)) + "\nargs = sys.argv[1:]\n" +
            "if any(value in ('build', 'test', 'aquery', 'cquery') for value in args):\n" +
            "    args.extend(" + repr(flags) + ")\n" +
            "os.execv(binary, [binary, '--output_base=' + base, '--host_jvm_args=-Xmx" +
            str(profile["jvm_heap_gib"]) + "g', " +
            "'--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts', *args])\n")


def p4rt_owner_identity(workspace):
    """Bind the existing source gitlink, clean owner and its distinct tool version."""
    require((workspace / ".bazelversion").read_text().strip() == "8.5.1", "P4RT root must use Bazel 8.5.1")
    require(output(["git", "-C", str(workspace), "ls-tree", "HEAD", "--", P4RT_OWNER_PATH]) ==
            "160000 commit " + P4RT_OWNER + "\t" + P4RT_OWNER_PATH, "P4RT owner gitlink changed")
    owner = workspace / P4RT_OWNER_PATH
    require(owner.resolve() == owner and owner.is_dir(), "P4RT owner path must not traverse symlinks")
    source_check(owner, P4RT_OWNER)
    require(not output(["git", "-C", str(owner), "status", "--porcelain"]), "P4RT owner checkout must be clean")
    require((owner / ".bazelversion").read_text().strip() == "6.4.0", "P4RT owner must use Bazel 6.4.0")
    for name in ("WORKSPACE.bazel", "ci/production_runtime.py"):
        require((owner / name).is_file(), "Missing P4RT owner interface: " + name)
    return owner


def owner_launcher_text(binary, base, rc, cache, remote_cache=None):
    # Keep the WORKSPACE owner's Bazel6 options separate from the Bazel8 launcher.
    extra, arguments = "", "*sys.argv[1:]"
    if remote_cache is not None:
        extra = ("args = sys.argv[1:]\n" +
                 "if any(value in ('build', 'test', 'aquery', 'cquery') for value in args):\n" +
                 "    args.extend(" + repr(remote_cache_options(remote_cache)) + ")\n")
        arguments = "*args"
    return ("#!/usr/bin/env python3\nimport os, sys\n" +
            "os.environ['USE_BAZEL_VERSION'] = '6.4.0'\n" +
            "os.environ['BAZELISK_HOME'] = " + repr(str(cache / "bazelisk")) + "\n" +
            "binary = " + repr(binary) + "\n" + extra +
            "os.execv(binary, [binary, " + repr("--output_base=" + str(base)) + ", " +
            "'--host_jvm_args=-Xmx2g', " +
            "'--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts', " +
            "'--nosystem_rc', '--nohome_rc', " + repr("--bazelrc=" + str(rc)) + ", " + arguments + "])\n")


def prepare_p4rt(workspace, cache, temporary, distdirs, mirror, remote_cache=None):
    require((workspace / ".bazelversion").read_text().strip() == "8.5.1", "P4RT root must use Bazel 8.5.1")
    require(output(["git", "-C", str(workspace), "ls-tree", "HEAD", "--", P4RT_OWNER_PATH]) ==
            "160000 commit " + P4RT_OWNER + "\t" + P4RT_OWNER_PATH, "P4RT owner gitlink changed")
    require(output(["git", "-C", str(workspace), "config", "-f", ".gitmodules", "--get",
                    "submodule." + P4RT_OWNER_PATH + ".url"]) == "https://github.com/securely1g/sonic-pins.git",
            "P4RT owner repository changed")
    run(["git", "-C", str(workspace), "submodule", "update", "--init", "--checkout", "--", P4RT_OWNER_PATH])
    owner = p4rt_owner_identity(workspace)
    owner_cache = cache / "p4rt-owner"
    require(owner_cache.resolve() == owner_cache, "P4RT owner cache must not traverse symlinks")
    names = ("repository", "bazelisk", "output-base") if remote_cache is not None else ("repository", "disk", "bazelisk", "output-base")
    for name in names:
        path = owner_cache / name
        require(path.resolve() == path, "P4RT owner cache child must not traverse symlinks")
        path.mkdir(parents=True, exist_ok=True)
    rc = temporary / "p4rt-owner.bazelrc"
    launch = temporary / "p4rt-owner-bazel"
    require(not rc.exists() and not launch.exists(), "P4RT owner launcher must be fresh")
    rc.write_text("\n".join([
        "common --repository_cache=" + ("" if remote_cache is not None else str(owner_cache / "repository")),
        "build --disk_cache=" + ("" if remote_cache is not None else str(owner_cache / "disk")),
        "build --local_ram_resources=4096",
        "common --experimental_downloader_config=" + str(mirror),
        *["common --distdir=" + str(directory) for directory in distdirs],
    ]) + "\n")
    base = owner_cache / "output-base"
    launch.write_text(owner_launcher_text(shutil.which("bazel"), base, rc, owner_cache, remote_cache))
    launch.chmod(0o755)
    return {"source": str(owner), "revision": P4RT_OWNER, "bazel": str(launch), "bazel_sha256": digest(launch),
            "rc": str(rc), "rc_sha256": digest(rc), "output_base": str(base), "jobs": 1}


def p4rt_context(args, state):
    context = state.get("p4rt_owner", {})
    owner = p4rt_owner_identity(args.workspace)
    cache = worker_work_directory(selected_worker_pool(), os.environ.get("RUNNER_NAME", "")) / "p4rt-owner"
    temporary = temporary_directory()
    require(context.get("source") == str(owner) and context.get("revision") == P4RT_OWNER and
            context.get("output_base") == str(cache / "output-base") and context.get("jobs") == 1 and
            context.get("bazel") == str(temporary / "p4rt-owner-bazel") and
            context.get("rc") == str(temporary / "p4rt-owner.bazelrc"), "P4RT prepared context changed")
    for name in ("bazel", "rc"):
        path = Path(context[name])
        require(not path.is_symlink() and digest(path) == context[name + "_sha256"], "P4RT launcher/configuration changed")
    index = P4RT_PHASES.index(args.phase)
    if index:
        prior = json.loads((args.artifacts / ("worker-" + P4RT_PHASES[index - 1] + ".json")).read_text())
        require(prior.get("phase") == P4RT_PHASES[index - 1] and prior.get("status") == "passed" and
                prior.get("source_revision") == args.revision and prior.get("image") == "p4rt" and
                prior.get("no_oom_events") is True, "P4RT predecessor phase has not passed")
    return context


def phase_command(image, phase, workspace, artifacts, owner=None):
    """Only the selected existing, fixed driver interfaces are executable."""
    require(image in DRIVERS and phase in (P4RT_PHASES if image == "p4rt" else
            ("build", "validate", "incremental", "loaded")), "Unknown driver phase")
    prefix = [sys.executable, "-B"]
    wrapper = str(workspace / "tools/bazel/ci" / WRAPPERS[image])
    if image == "p4rt":
        require(owner and owner.get("revision") == P4RT_OWNER and owner.get("jobs") == 1 and
                owner.get("source") == str(workspace / P4RT_OWNER_PATH), "Missing fixed P4RT owner context")
        common = ["--directory", str(artifacts), "--owner-source", owner["source"]]
        if phase == "loaded":
            return prefix + [str(workspace / "tools/bazel/ci/p4rt_loaded.py"), *common]
        if phase in ("validate", "incremental"):
            return prefix + [str(workspace / "tools/bazel/ci/p4rt_validate.py"), phase, *common, "--bazel", wrapper]
        return prefix + [str(workspace / "tools/bazel/ci/p4rt_image.py"), phase, *common,
                         "--bazel", wrapper, "--owner-revision", owner["revision"],
                         "--owner-bazel", owner["bazel"], "--owner-output-base", owner["output_base"], "--jobs", "1"]
    if phase == "loaded":
        return prefix + [str(workspace / f"tools/bazel/ci/{DRIVERS[image]}_loaded.py"), "--artifacts", str(artifacts)]
    if phase == "incremental" and image == "orchagent":
        return prefix + [str(workspace / "tools/bazel/ci/swss_rebuild.py"), "--bazel", wrapper,
                         "--runtime-target", "//dockers/docker-orchagent:docker-orchagent.gz",
                         "--additional-runtime-target", "//dockers/docker-orchagent:docker-orchagent-dbg.gz",
                         "--additional-runtime-target", "//dockers/docker-orchagent:rdeps",
                         "--symbols-target", "//dockers/docker-orchagent:rdeps_debug_symbols",
                         "--unchanged-target", "//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz",
                         "--unchanged-target", "@scapy//:wheel", "--output", str(artifacts / "rebuild")]
    command = prefix + [str(workspace / f"tools/bazel/ci/{DRIVERS[image]}_image.py"), phase,
                        "--artifacts", str(artifacts)]
    if image in GNMI_PROFILES:
        command += ["--dialout", GNMI_PROFILES[image]]
    return command + (["--bazel", wrapper] if phase != "validate" else [])


def temporary_directory():
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    name = os.environ.get("SOURCE_WORKER_SCRATCH", "")
    if not name:
        return root
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    runner = os.environ.get("RUNNER_NAME", "")
    require(all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt)) and
            worker_matches_pool(runner, selected_worker_pool()), "Invalid worker scratch identity")
    require(name == f"source-image-scratch-{run_id}-{attempt}-{runner}", "Worker scratch path differs from this run")
    path = root / name
    require(not path.is_symlink() and path.resolve() == path, "Worker scratch path must not traverse symlinks")
    return path



def configure_worker_mirrors(phase, artifacts, *, config=None, public=None, image=None):
    config = config or temporary_directory() / "source-image-mirrors.cfg"
    if phase == "cleanup-mirrors":
        config.unlink(missing_ok=True)
        return
    registry_ci = ROOT / ".source-mirror-registry" / "ci"
    sys.path.insert(0, str(registry_ci))
    import private_mirrors
    roots = {key: os.environ.pop(key, "") for key, _ in private_mirrors.MIRROR_ROOTS}
    budget = None
    if phase == "scan-mirrors" and image == "p4rt":
        from mirror_privacy import TemporaryBudget
        directory = artifacts / "mirror-scan-temporaries"
        directory.mkdir(mode=0o700)
        budget = TemporaryBudget(directory, maximum_bytes=32 * 1024 ** 3, minimum_free_bytes=16 * 1024 ** 3)
    redactor = private_mirrors.Redactor([root for root in roots.values() if root],
                                      **({"temporary_budget": budget} if budget else {}))
    redactor.mask_github()
    if phase == "scan-mirrors":
        public = public or artifacts / "public"
        try:
            redactor.scrub_and_scan(public)
            # Published image receipts bind their evidence and payloads by hash.
            require(not (public / "mirror-redaction.json").exists(), "Private mirror location in staged image evidence")
        finally:
            if budget:
                # Scalar diagnostics survive a failed scan; errors still fail the step.
                print("Mirror scan temporary budget: " + json.dumps(budget.usage()), file=sys.stderr)
        if budget:
            usage = budget.usage()
            require(all(type(value) is int and value >= 0 for value in usage.values()) and
                    usage["live_bytes"] == usage["open_files"] == 0,
                    "Scanner temporary files did not close cleanly")
            measured = bool(redactor.patterns)
            metrics = {"status": "passed" if measured else "not_run_no_private_patterns",
                       "traversal_measured": measured,
                       "maximum_bytes": usage["maximum_bytes"], "minimum_free_bytes": usage["minimum_free_bytes"]}
            if measured:
                metrics.update(peak_charged_temporary_bytes=usage["peak_bytes"],
                               peak_open_files=usage["peak_open_files"], live_bytes=0, open_files=0)
            path = public / "worker.json"
            require(path.is_file() and not path.is_symlink(), "Missing regular worker receipt")
            receipt = json.loads(path.read_text())
            require(receipt.get("image") == "p4rt" and "mirror_scan" not in receipt,
                    "Unexpected worker scan receipt")
            receipt["mirror_scan"] = metrics
            safe_json(receipt)
            # Export copied this small JSON; the later CI bundle binds its final hash.
            path.write_text(json.dumps(receipt, indent=2) + "\n")
        return
    if selected_worker_pool() == "sonic-slave-container-k3s":
        require(not any(roots.values()), "K3s source workers use only the existing public downloader")
    mirrors = private_mirrors.selected_mirrors(roots)
    if mirrors:
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
                os.environ.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage" and
                os.environ.get("GITHUB_ACTOR") == "securely1g" and
                platform.machine() == "x86_64" and
                platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie" and
                re.fullmatch(r"sonic-slave-lgh114-[1-4]", os.environ.get("RUNNER_NAME", "")),
                "Private Debian mirrors require a trusted native Trixie worker")
    public = mirror_configuration((registry_ci / "source_mirrors.cfg").read_bytes()).decode()
    private_mirrors.write_config(config, private_mirrors.configuration(public, mirrors) if mirrors else public)


def prepare(args):
    workspace, artifacts = args.workspace, args.artifacts
    if args.image == "teamd":
        prepare_teamd_kernel(artifacts)
    pool = selected_worker_pool()
    if pool == "sonic-slave-container-k3s":
        managed_cache_helper()
    preflight = measure(os.environ.get("RUNNER_NAME", ""), pool)
    (artifacts / "worker-preflight.json").write_text(json.dumps(preflight, indent=2) + "\n")
    require(preflight["status"] == "passed", "Worker resource/capability preflight failed")
    profile = preflight["build_profile"]
    if pool == "sonic-slave-container-k3s":
        require(profile == select_build_profile(20 * 1024**3, 6),
                "K3s worker limits differ from the measured two-action profile")
    require(os.getuid() == 1000 and os.getgid() == 1000, "Expected worker UID/GID1000")
    require(platform.machine() == "x86_64" and
            platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie", "Expected native AMD64 Debian Trixie")
    require(Path("/.dockerenv").is_file() and workspace.is_relative_to("/actions/_work"), "Expected a private SONiC slave workspace")
    require(not any(os.environ.get(k) for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"))
            and not Path("/var/run/docker.sock").exists(), "Refuse an inherited Docker daemon")
    runner = os.environ.get("RUNNER_NAME", "")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", runner) and runner not in (".", ".."), "Invalid worker name")
    marker = Path("/etc/sonic-slave-github-runner.json")
    info = marker.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o222, "Runner marker must be root-owned and read-only")
    marker_data = json.loads(marker.read_text())
    require(all(marker_data.get(k) == v for k, v in {"uid": 1000, "gid": 1000, "work_root": "/actions/_work"}.items()), "Unexpected worker profile")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", marker_data.get("base_image", "")), "Missing immutable slave image identity")
    for name in ("bazel", "docker", "dockerd", "sudo", "git", "make", "readelf", "mountpoint", "findmnt"):
        require(shutil.which(name), "Missing worker tool: " + name)
    require(Path("/etc/ssl/certs/java/cacerts").is_file() and os.access("/etc/ssl/certs/java/cacerts", os.R_OK),
            "Missing readable system Java trust store")
    run(["sudo", "-n", "true"])
    for mount in (("/actions", "/bazel_cache") if pool == "sonic-slave-container-dpool" else ("/actions",)):
        run(["mountpoint", "-q", mount])
    require(output(["findmnt", "-n", "-o", "FSTYPE", "-T", "/actions"]) in ("ext4", "xfs"), "Docker storage requires ext4 or XFS")
    if args.image == "syncd":
        prepare_tap_fixture(artifacts)
    prepare_validation_tools(artifacts)
    source_check(workspace, args.revision)
    for name in (f"{DRIVERS[args.image]}_image.py", f"{DRIVERS[args.image]}_loaded.py", WRAPPERS[args.image], "review_bazel.sh"):
        path = workspace / "tools/bazel/ci" / name
        require(path.is_file() and path.resolve().is_relative_to(workspace), "Missing reviewed driver: " + name)
    for name in BUILD_CHECKS.get(args.image, ()):
        path = workspace / "tools/bazel/tests" / name
        require(path.is_file() and path.resolve().is_relative_to(workspace), "Missing reviewed image check: " + name)
    if args.image == "p4rt":
        for name in ("p4rt_validate.py", "p4rt_handoff.py", "p4rt_common_sdk.py"):
            require((workspace / "tools/bazel/ci" / name).is_file(), "Missing reviewed P4RT interface: " + name)
    catalog = catalog_for(args.image, workspace, os.environ.get("SOURCE_REGISTRY_REVISION", ""))
    if args.image == "sysmgr":
        run(["git", "-C", str(workspace), "submodule", "update", "--init", "--", "src/sonic-sysmgr/gnoi"])
    cache = prepare_work_directory(pool, runner, args.image)
    remote_cache = prepare_remote_cache(workspace, cache) if pool == "sonic-slave-container-k3s" else None
    temporary = temporary_directory()
    require(temporary.is_relative_to("/actions/_work"), "Expected a worker-private temporary directory")
    temporary.mkdir(parents=True, exist_ok=True)
    distdirs, download_receipt = prepare_source_downloads(temporary / "source-image-inputs", cache)
    (artifacts / "worker-source-downloads.json").write_text(json.dumps(download_receipt, indent=2) + "\n")
    launch = temporary / "source-image-bazel"
    launch.write_text(launcher_text(shutil.which("bazel"), cache / args.image / "output-base", profile, remote_cache))
    launch.chmod(0o755)
    mirror = temporary / "source-image-mirrors.cfg"
    if os.environ.get("SOURCE_WORKER_CONFIGURED_MIRRORS") == "1":
        require(mirror.is_file() and not mirror.is_symlink() and stat.S_IMODE(mirror.stat().st_mode) == 0o600,
                "Expected this job's prepared Debian mirror configuration")
    else:
        with urllib.request.urlopen(MIRROR_URL, timeout=60) as response:
            mirror.write_bytes(mirror_configuration(response.read()))
    rc = workspace / ".bazelrc.user"
    require(not rc.exists(), "Expected a fresh source checkout without a local Bazel override")
    rc.write_text("\n".join([
        ("# Remote action cache with fresh run-attempt working state; no Make package cache." if remote_cache is not None else
         "# Private persistent source-image worker caches; no Make package cache."),
        "common --repository_cache=" + ("" if remote_cache is not None else str(cache / "repository")),
        "common --disk_cache=" + ("" if remote_cache is not None else str(cache / "disk")),
        "common --experimental_downloader_config=" + str(mirror),
        *["common --distdir=" + str(directory) for directory in distdirs],
    ]) + "\n")
    run_key = os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[0-9]+-[0-9]+", run_key), "Missing workflow run identity")
    docker_root = temporary / ("source-image-docker-" + run_key)
    pidfile = temporary / ("source-image-dockerd-" + run_key + ".pid")
    require(not docker_root.exists() and not pidfile.exists(), "Private daemon state must be fresh")
    client = temporary / "source-image-docker-client"
    client.mkdir(mode=0o700)
    daemon_config = temporary / "source-image-dockerd.json"
    daemon_config.write_text("{}\n")
    env = {"BAZEL_REAL": str(launch), "BAZELISK_HOME": str(cache / "bazelisk"),
           "SONIC_BAZEL_REGISTRY_REVISION": catalog, "CI_HEAD_SHA": args.revision, "CI_BASE_SHA": "",
           "DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONFIG": str(client), "BAZELISK_GITHUB_TOKEN": ""}
    state = {"image": args.image, "revision": args.revision, "workspace": str(workspace), "environment": env,
             "pidfile": str(pidfile), "docker_root": str(docker_root), "build_profile": profile}
    if remote_cache is not None:
        state["remote_cache"] = remote_cache
    if args.image == "p4rt":
        state["p4rt_owner"] = prepare_p4rt(workspace, cache, temporary, distdirs, mirror, remote_cache)
        env["USE_BAZEL_VERSION"] = "8.5.1"
    (artifacts / "worker-private.json").write_text(json.dumps(state, indent=2) + "\n")
    run(["sudo", "-n", sys.executable, str(ROOT / "tools/bazel/ci/prepare_nested_docker.py")])
    capture_docker_mount(args, "before")
    with (artifacts / "dockerd.log").open("w") as log:
        subprocess.Popen(["sudo", "-n", "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root",
                          shutil.which("dockerd"), "--config-file=" + str(daemon_config),
                          "--host=" + env["DOCKER_HOST"], "--group=docker", "--data-root=" + str(docker_root),
                          "--exec-root=/run/sonic-source-image-docker", "--pidfile=" + str(pidfile),
                          "--storage-driver=overlay2", "--exec-opt=native.cgroupdriver=cgroupfs",
                          "--bridge=none", "--iptables=false", "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"],
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"],
                                   env={**os.environ, **env}, text=True, capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if probe.returncode == 0:
            require(probe.stdout.strip() == str(docker_root), "Docker data root differs")
            break
        time.sleep(1)
    else:
        raise ValueError("Private Docker did not become ready")
    capture_docker_mount(args, "ready")
    receipt = {"schema": 3 if remote_cache is not None else 1, "status": "prepared", "worker": runner, "architecture": "amd64", "distribution": "trixie",
               "source_revision": args.revision, "dispatcher_revision": output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
               "review_catalog": catalog, "slave_image": marker_data["base_image"], "cache_mode": "remote_only_run_attempt" if remote_cache is not None else "persistent_worker_local",
               "build_profile": profile, "memory": memory_snapshot(),
               "docker_network": "none", "image": args.image}
    if remote_cache is not None:
        receipt["remote_cache"] = dict(K3S_MANAGED_REMOTE_CACHE_POLICY, endpoint=remote_cache["endpoint"], host=remote_cache["host"])
    if args.image == "p4rt":
        receipt["owner_profile"] = owner_build_profile(pool)
    safe_json(receipt)
    (artifacts / "worker.json").write_text(json.dumps(receipt, indent=2) + "\n")


def state_for(args):
    state = json.loads((args.artifacts / "worker-private.json").read_text())
    require(state["image"] == args.image and state["revision"] == args.revision and state["workspace"] == str(args.workspace),
            "Worker preparation does not match this selection")
    catalog = os.environ.get("SOURCE_REGISTRY_REVISION", "")
    require(not catalog or state["environment"]["SONIC_BAZEL_REGISTRY_REVISION"] == catalog,
            "Prepared catalog differs from the requested source/catalog pair")
    return state


def prepared_profile(state, memory):
    profile = select_build_profile(int(memory["limit"]), cpu_limit(memory["cpu_max"]))
    require(state.get("build_profile") == profile, "Worker resources/profile differ from preparation")
    return profile


def execute_phase(args):
    state = state_for(args)
    source_check(args.workspace, args.revision)
    if selected_worker_pool() == "sonic-slave-container-k3s":
        require(state.get("remote_cache") == remote_cache_configuration(args.workspace),
                "Prepared remote-cache configuration changed")
    owner = p4rt_context(args, state) if args.image == "p4rt" else None
    before = memory_snapshot()
    profile = prepared_profile(state, before)
    if args.phase == "loaded":
        # The worker's own server is idle here; disk/output caches remain intact.
        run([state["environment"]["BAZEL_REAL"], "shutdown"], cwd=args.workspace,
            env={**os.environ, **state["environment"]}, timeout=90)
    started = time.monotonic()
    finished = threading.Event()

    def sample():
        while not finished.wait(30):
            print(json.dumps({"phase": args.phase, "elapsed_seconds": round(time.monotonic() - started),
                              "configured_graph_audited": (args.artifacts / "action-audit.json").is_file(),
                              "memory": memory_snapshot()}), flush=True)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        checks = BUILD_CHECKS.get(args.image, ()) if args.phase == "build" else ()
        commands = [[sys.executable, "-B", str(args.workspace / "tools/bazel/tests" / name)] for name in checks]
        commands.append(phase_command(args.image, args.phase, args.workspace, args.artifacts, owner) if owner else
                        phase_command(args.image, args.phase, args.workspace, args.artifacts))
        for command in commands:
            completed = subprocess.run(command, cwd=args.workspace,
                                       env={**os.environ, **state["environment"]})
            if completed.returncode:
                break
    finally:
        finished.set()
        monitor.join()
    source_check(args.workspace, args.revision)
    if owner:
        p4rt_owner_identity(args.workspace)
    after = memory_snapshot()
    memory_ok = all(after["events"].get(key, 0) == before["events"].get(key, 0) for key in ("oom", "oom_kill"))
    receipt = {"phase": args.phase, "status": "passed" if completed.returncode == 0 and memory_ok else "failed",
               "source_revision": args.revision, "image": args.image, "elapsed_seconds": round(time.monotonic() - started, 3),
               "exit_code": completed.returncode, "memory_before": before, "memory_after": after,
               "build_profile": profile, "no_oom_events": memory_ok}
    if owner:
        receipt["owner_profile"] = owner_build_profile(selected_worker_pool())
    (args.artifacts / ("worker-" + args.phase + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
    require(completed.returncode == 0, "Image phase failed: " + args.phase)
    require(memory_ok, "Worker exhausted its memory during " + args.phase)


def publish(args):
    """Stage only declared payloads and checked public evidence, never private logs/env."""
    if getattr(args, "image", None) == "p4rt":
        from tools.bazel.ci.p4rt_public import prepare as prepare_public
        return prepare_public(args.artifacts)
    source, destination = args.artifacts, args.artifacts / "public"
    destination.mkdir(exist_ok=True)
    receipt_path = source / "build-receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if getattr(args, "image", None) in GNMI_PROFILES:
            require(receipt.get("dialout_enabled") is (GNMI_PROFILES[args.image] == "enabled"),
                    "gNMI receipt differs from the selected fixed dialout profile")
        public = {k: receipt[k] for k in ("status", "architecture", "revision", "tested_revision_parents", "dialout_enabled", "artifacts") if k in receipt}
        safe_json(public)
        (destination / "public-receipt.json").write_text(json.dumps(public, indent=2) + "\n")
        for name, item in receipt.get("artifacts", {}).items():
            require(Path(name).name == name and name not in (".", ".."), "Invalid artifact filename")
            path = source / name
            require(path.is_file() and not path.is_symlink() and path.stat().st_size == item["bytes"] and digest(path) == item["sha256"],
                    "Declared artifact differs: " + name)
            if path.suffix == ".json":
                value = json.loads(path.read_text())
                if name == "policy.json" and getattr(args, "image", None) in ("syncd", "gearbox"):
                    # Reviewed image policy, not a captured process environment.
                    # Validate only this known public constant; retain original bytes.
                    require(value["image_validation"].pop("environment") == {"DEBIAN_FRONTEND": "noninteractive"},
                            "Unexpected image policy environment")
                safe_json(value)
            target = destination / name
            if target.exists():
                require(target.is_file() and not target.is_symlink() and digest(target) == item["sha256"],
                        "Existing public artifact differs: " + name)
            else:
                os.link(path, target)
    names = ["worker.json", "action-audit.json", "validation.json", "loaded-validation.json", "incremental.json", "rebuild/rebuild.json"]
    names += [p.name for pattern in ("worker-*.json", "*.validation.json", "*.runtime.json") for p in source.glob(pattern)
              if p.name != "worker-private.json"]
    for name in sorted(set(names)):
        path = source / name
        if path.is_file():
            safe_json(json.loads(path.read_text()))
            target = destination / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
    for name, kind in (("MODULE.bazel.lock", "lock"), ("module-graph.json", "graph")):
        path = source / name
        if path.is_file():
            dependency_json(json.loads(path.read_text()), kind)
            shutil.copyfile(path, destination / name)
    summary = {str(path.relative_to(source)): bep_summary(path.read_text().splitlines(), partial=True)
               for path in source.rglob("*events.json*") if not path.is_relative_to(destination)}
    safe_json(summary)
    (destination / "build-summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def docker_mount_scope(args, state):
    root, pidfile = Path(state["docker_root"]), Path(state["pidfile"])
    temporary = temporary_directory()
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    runner = os.environ.get("RUNNER_NAME", "")
    require(all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt)) and
            worker_matches_pool(runner, selected_worker_pool()), "Missing owned Docker run/worker identity")
    require(root.parent == temporary and temporary.is_relative_to("/actions/_work") and
            root.name == f"source-image-docker-{run_id}-{attempt}", "Unexpected proved Docker root")
    environment = state["environment"]
    endpoint, client = environment.get("DOCKER_HOST", ""), Path(environment.get("DOCKER_CONFIG", ""))
    require(endpoint.startswith("unix:///") and client.is_absolute() and client.is_relative_to(temporary) and
            client.resolve() == client, "Expected exact private Docker endpoint/configuration")
    return {"root": str(root), "pidfile": str(pidfile), "endpoint": endpoint, "client": str(client),
            "run": run_id, "attempt": attempt, "worker": runner,
            "image": args.image, "revision": args.revision, "workspace": str(args.workspace)}


def run_docker_mount_action(args, state, action):
    require(action in ("before", "ready", "inventory", "stop", "cleanup"), "Unknown private Docker mount action")
    scope = docker_mount_scope(args, state)
    return run(["sudo", "-n", sys.executable, "-c", docker_owned_mount_script(), action,
                state["docker_root"], state["pidfile"], json.dumps(scope, sort_keys=True)],
               timeout=120 if action == "cleanup" else (70 if action == "stop" else 25))


def capture_docker_mount(args, stage):
    require(stage in ("before", "ready"), "Unknown private Docker capture stage")
    state = state_for(args)
    require("docker_mount_protocol" not in state, "Do not replay a completed Docker mount capture")
    run_docker_mount_action(args, state, stage)
    if stage == "ready":
        state["docker_mount_protocol"] = 1
        (args.artifacts / "worker-private.json").write_text(json.dumps(state, indent=2) + "\n")


def stop_with_mount_proof(args, state):
    docker_mount_scope(args, state)
    pidfile = Path(state["pidfile"])
    if not pidfile.exists():
        require(not Path(state["docker_root"]).exists(), "Daemon left data without a PID; retain it for diagnosis")
        return
    inventory_failure = None
    try:
        run_docker_mount_action(args, state, "inventory")
    except BaseException as error:
        inventory_failure = error
    # A failed inventory never bypasses generation proof or skips an owned stop attempt.
    try:
        run_docker_mount_action(args, state, "stop")
    except BaseException as stop_error:
        if inventory_failure is not None:
            raise stop_error from inventory_failure
        raise
    if inventory_failure is not None:
        raise inventory_failure
    run_docker_mount_action(args, state, "cleanup")
    pidfile.unlink(missing_ok=True)



def stop_selected(args):
    if not (args.artifacts / "worker-private.json").exists():
        return stop(args)
    state = state_for(args)
    if "docker_mount_protocol" not in state:
        return stop(args)
    require(type(state["docker_mount_protocol"]) is int and state["docker_mount_protocol"] == 1,
            "Unknown Docker mount protocol; retain state")
    return stop_with_mount_proof(args, state)


def docker_owned_mount_script():
    return r'''import grp, hashlib, json, os, re, resource, shutil, signal, stat, subprocess, sys, time
from pathlib import Path

CAP = 65536

def require(value, reason):
    if not value:
        raise ValueError(reason)

def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'Duplicate private mount proof field')
        result[key] = value
    return result

def decode(data):
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite proof')))

def directory(path):
    value = path.lstat()
    require(stat.S_ISDIR(value.st_mode) and path.resolve() == path, 'Private mount directory identity')
    return {'device': value.st_dev, 'inode': value.st_ino, 'uid': value.st_uid,
            'gid': value.st_gid, 'mode': stat.S_IMODE(value.st_mode)}

def namespace():
    value = os.stat('/proc/self/ns/mnt')
    return [value.st_dev, value.st_ino]

def read_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and
                stat.S_IMODE(before.st_mode) == 0o444 and 0 < before.st_size <= CAP,
                'Private mount proof must be a bounded root-owned immutable file')
        data = os.read(fd, CAP + 1)
        after = os.fstat(fd)
        require(len(data) == before.st_size and
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                'Private mount proof changed')
        return decode(data), hashlib.sha256(data).hexdigest()
    finally:
        os.close(fd)

def write_record(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    require(len(data) <= CAP, 'Private mount proof size')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fchmod(fd, 0o444)
    finally:
        os.close(fd)

def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)

def parse_mounts(data):
    require(len(data) <= 1024 * 1024, 'Mount table exceeds bound')
    rows = []
    for line in data.splitlines():
        left, right = line.split(' - ', 1)
        fields, trailing = left.split(), right.split()
        require(len(fields) >= 6 and len(trailing) >= 3 and
                re.fullmatch(r'[0-9]+:[0-9]+', fields[2]), 'Invalid mount row')
        root, point = unescape(fields[3]), unescape(fields[4])
        require(point.startswith('/') and not point.startswith('//') and
                '..' not in Path(point).parts and str(Path(point)) == point,
                'Invalid namespace mountpoint')
        require(root.startswith('/'), 'Invalid filesystem mount root')
        # cgroup_namespaces(7): field 4 may be /.. in a new cgroup namespace.
        # This is filesystem-root metadata, never a path to traverse here.
        require('..' not in Path(root).parts or trailing[0] in ('cgroup', 'cgroup2'),
                'Parent components in non-cgroup mount root')
        rows.append({'id': int(fields[0]), 'parent': int(fields[1]), 'device': fields[2],
                     'root': root, 'point': point, 'options': fields[5], 'optional': fields[6:],
                     'filesystem': trailing[0], 'source': unescape(trailing[1]), 'super': trailing[2:]})
    require(len(rows) <= 4096 and len({row['id'] for row in rows}) == len(rows), 'Mount table identity/bound')
    return rows

def mounts():
    with open('/proc/self/mountinfo', 'r', encoding='utf-8') as stream:
        return parse_mounts(stream.read(1024 * 1024 + 1))

def below(rows, root):
    return [row for row in rows if Path(row['point']).is_relative_to(root)]

def canonical_mount_root(row):
    value = row['root']
    require(value.startswith('/') and not value.startswith('//') and
            '..' not in Path(value).parts and str(Path(value)) == value,
            'Selected mount root is not canonical')

def covering(rows, root):
    choices = [row for row in rows if root.is_relative_to(Path(row['point']))]
    require(choices, 'Missing backing mount')
    depth = max(len(Path(row['point']).parts) for row in choices)
    choices = [row for row in choices if len(Path(row['point']).parts) == depth]
    require(len(choices) == 1, 'Ambiguous backing mount')
    canonical_mount_root(choices[0])
    return choices[0]

def self_bind(before, row, root):
    parent = before['covering']
    canonical_mount_root(parent)
    canonical_mount_root(row)
    expected_root = str(Path(parent['root']) / root.relative_to(Path(parent['point'])))
    require(row['point'] == str(root) and row['parent'] == parent['id'] and
            row['id'] not in before['mount_ids'] and row['device'] == parent['device'] and
            row['filesystem'] == parent['filesystem'] and row['source'] == parent['source'] and
            row['root'] == expected_root and row['filesystem'] in ('ext4', 'xfs'),
            'Root mount is not the captured new self-bind')
    require(not any(item.startswith(('shared:', 'master:')) for item in parent['optional']) and
            len(row['optional']) == 1 and re.fullmatch(r'shared:[0-9]+', row['optional'][0]),
            'Unexpected root mount propagation')

def tools_ready():
    for name in ('/usr/bin/docker', '/usr/bin/umount'):
        value = Path(name).lstat()
        require(stat.S_ISREG(value.st_mode) and value.st_uid == 0 and
                not value.st_mode & 0o022 and os.access(name, os.X_OK),
                'Required fixed Docker/umount executable is unavailable')
    require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'),
            'Owned daemon signaling requires pidfd support')

def process_start(pid):
    require(type(pid) is int and pid > 1, 'Invalid owned process ID')
    with (Path('/proc') / str(pid) / 'stat').open('r') as stream:
        raw = stream.read(4097)
    require(len(raw) <= 4096, 'Owned process stat exceeds bound')
    return int(raw.rsplit(') ', 1)[1].split()[19])

def process_identity(pid):
    start = process_start(pid)
    mount_ns = (Path('/proc') / str(pid) / 'ns/mnt').stat()
    require(process_start(pid) == start, 'Process changed while reading namespace')
    return {'pid': pid, 'start_ticks': start,
            'namespace': [mount_ns.st_dev, mount_ns.st_ino]}

def child_ids(parent):
    # Enumerate only tasks of this already proved process, never global /proc.
    identifiers = set()
    count = 0
    with os.scandir('/proc/' + str(parent['pid']) + '/task') as tasks:
        for task in tasks:
            count += 1
            require(count <= 512 and task.name.isdecimal(), 'Owned task enumeration bound')
            with open(task.path + '/children', 'r') as stream:
                data = stream.read(4097)
            require(len(data) <= 4096, 'Owned child list exceeds bound')
            for raw in data.split():
                require(raw.isdecimal() and int(raw) > 1, 'Invalid owned child ID')
                identifiers.add(int(raw))
                require(len(identifiers) <= 128, 'Owned child count exceeds bound')
    require(process_identity(parent['pid']) == parent, 'Parent generation changed during child capture')
    return sorted(identifiers)

def descendants(parent):
    found = {}
    pending = [(parent, 0)]
    deadline = time.monotonic() + 5
    while pending:
        current, depth = pending.pop()
        require(depth <= 8 and time.monotonic() < deadline, 'Owned descendant traversal bound')
        for pid in child_ids(current):
            require(pid != parent['pid'] and pid not in found and len(found) < 128,
                    'Owned descendant identity/count differs')
            value = process_identity(pid)
            found[pid] = value
            pending.append((value, depth + 1))
    require(process_identity(parent['pid']) == parent, 'Daemon changed during descendant capture')
    return [found[key] for key in sorted(found)]

def merge_descendants(records, additions):
    for value in additions:
        pid = value['pid']
        require(pid not in records or records[pid] == value, 'Owned descendant generation changed')
        records[pid] = value
        require(len(records) <= 128, 'Owned descendant count exceeds bound')

def require_closed(records):
    require(all(not (Path('/proc') / str(value['pid'])).exists() for value in records),
            'Observed owned process still exists; retain data')

def daemon(pidfile, root, expected=None):
    raw = pidfile.read_text().strip()
    require(raw.isdecimal() and int(raw) > 1, 'Invalid owned daemon PID')
    path = Path('/proc') / raw
    require((path / 'comm').read_text().strip() == 'dockerd' and
            ('--data-root=' + str(root)).encode() in (path / 'cmdline').read_bytes().split(b'\0'),
            'Daemon identity changed')
    result = process_identity(int(raw))
    require(result['namespace'] == namespace() and (expected is None or result == expected),
            'Daemon generation or mount namespace changed')
    return result


def load_proof(root, scope):
    before, before_hash = read_record(root.with_name(root.name + '.mount-before.json'))
    ready, ready_hash = read_record(root.with_name(root.name + '.mount-ready.json'))
    canonical_mount_root(before['covering'])
    require(before['schema'] == ready['schema'] == 1 and before['scope'] == ready['scope'] == scope and
            before['root_absent'] is True and ready['directory']['uid'] == 0 and
            ready['before_sha256'] == before_hash and before['namespace'] == ready['namespace'] == namespace() and
            before['parent'] == ready['parent'] == directory(root.parent) and
            ready['directory'] == directory(root), 'Owned mount proof identity changed')
    if ready['root_mount'] is not None:
        self_bind(before, ready['root_mount'], root)
    return before, ready, ready_hash

def capture(stage, root, pidfile, scope):
    rows = mounts()
    if stage == 'before':
        tools_ready()
        require(not os.path.lexists(root) and not below(rows, root), 'Docker root must be absent before launch')
        value = {'schema': 1, 'scope': scope, 'namespace': namespace(), 'parent': directory(root.parent),
                 'covering': covering(rows, root), 'mount_ids': [row['id'] for row in rows], 'root_absent': True}
        write_record(root.with_name(root.name + '.mount-before.json'), value)
        return
    before, digest = read_record(root.with_name(root.name + '.mount-before.json'))
    canonical_mount_root(before['covering'])
    require(before['schema'] == 1 and before['scope'] == scope and before['root_absent'] is True and
            before['namespace'] == namespace() and before['parent'] == directory(root.parent),
            'Startup mount context changed')
    parent = next((row for row in rows if row['id'] == before['covering']['id']), None)
    require(parent == before['covering'], 'Backing mount changed during startup')
    found = below(rows, root)
    require(len(found) <= 1 and (not found or found[0]['point'] == str(root)),
            'Unexpected descendant mounts at readiness')
    if found:
        self_bind(before, found[0], root)
    current = directory(root)
    require(current['uid'] == 0 and f"{os.major(current['device'])}:{os.minor(current['device'])}" == parent['device'],
            'Docker root storage/owner differs')
    value = {'schema': 1, 'scope': scope, 'before_sha256': digest, 'namespace': namespace(),
             'parent': directory(root.parent), 'directory': current, 'root_mount': found[0] if found else None,
             'daemon': daemon(pidfile, root)}
    value['descendants'] = descendants(value['daemon'])
    daemon(pidfile, root, value['daemon'])
    write_record(root.with_name(root.name + '.mount-ready.json'), value)

def file_limit():
    resource.setrlimit(resource.RLIMIT_FSIZE, (CAP, CAP))

def empty_inventory(value, root):
    require(isinstance(value, dict) and set(value) == {'root', 'containers', 'running', 'paused', 'stopped'} and
            value['root'] == str(root) and all(type(value[key]) is int and value[key] == 0
            for key in ('containers', 'running', 'paused', 'stopped')), 'Owned daemon inventory is not empty')

def inventory(root, pidfile, scope):
    before, ready, ready_hash = load_proof(root, scope)
    current = daemon(pidfile, root, ready['daemon'])
    observed = descendants(current)
    endpoint, client = scope['endpoint'], scope['client']
    require(endpoint.startswith('unix:///') and client.startswith('/'), 'Private Docker endpoint/config required')
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/home/admin',
                   'DOCKER_HOST': endpoint, 'DOCKER_CONFIG': client, 'LC_ALL': 'C'}
    fmt = '{"root":{{json .DockerRootDir}},"containers":{{.Containers}},"running":{{.ContainersRunning}},"paused":{{.ContainersPaused}},"stopped":{{.ContainersStopped}}}'
    stdout = root.with_name(root.name + '.empty-inventory.stdout')
    stderr = root.with_name(root.name + '.empty-inventory.stderr')
    with stdout.open('xb') as out, stderr.open('xb') as err:
        result = subprocess.run(['/usr/bin/docker', 'info', '--format', fmt], env=environment,
                                user=1000, group=1000, extra_groups=[grp.getgrnam('docker').gr_gid],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                timeout=10, preexec_fn=file_limit, close_fds=True)
    data, errors = stdout.read_bytes(), stderr.read_bytes()
    require(result.returncode == 0 and len(data) <= CAP and len(errors) <= CAP, 'Private Docker inventory failed')
    value = decode(data)
    empty_inventory(value, root)
    daemon(pidfile, root, current)
    write_record(root.with_name(root.name + '.empty-inventory.json'),
                 {'schema': 1, 'scope': scope, 'ready_sha256': ready_hash, 'daemon': current, 'descendants': observed,
                  'monotonic': time.monotonic(), 'inventory': value,
                  'stdout_sha256': hashlib.sha256(data).hexdigest(), 'stderr_sha256': hashlib.sha256(errors).hexdigest()})

def stop_daemon(root, pidfile, scope):
    before, ready, ready_hash = load_proof(root, scope)
    current = daemon(pidfile, root, ready['daemon'])
    # Open a kernel handle, then re-prove the generation before signaling it.
    handle = os.pidfd_open(current['pid'], 0)
    try:
        daemon(pidfile, root, current)
        observed = {}
        merge_descendants(observed, ready['descendants'])
        capture_failure = None
        try:
            inventory_path = root.with_name(root.name + '.empty-inventory.json')
            if os.path.lexists(inventory_path):
                value, unused = read_record(inventory_path)
                require(value['schema'] == 1 and value['scope'] == scope and
                        value['ready_sha256'] == ready_hash and value['daemon'] == current,
                        'Inventory process observation differs')
                merge_descendants(observed, value['descendants'])
            merge_descendants(observed, descendants(current))
        except Exception as error:
            capture_failure = error
        daemon(pidfile, root, current)
        signal.pidfd_send_signal(handle, signal.SIGTERM, None, 0)
        deadline = time.monotonic() + 60
        while (Path('/proc') / str(current['pid'])).exists():
            try:
                require(process_start(current['pid']) == current['start_ticks'], 'Daemon generation changed during stop')
            except FileNotFoundError:
                require(not (Path('/proc') / str(current['pid'])).exists(), 'Uncertain daemon state during stop')
                break
            require(time.monotonic() < deadline, 'Private Docker has not stopped; retain its data')
            time.sleep(0.5)
        while any((Path('/proc') / str(value['pid'])).exists() for value in observed.values()):
            for value in observed.values():
                if (Path('/proc') / str(value['pid'])).exists():
                    try:
                        require(process_start(value['pid']) == value['start_ticks'], 'Owned descendant identity changed during stop')
                    except FileNotFoundError:
                        require(not (Path('/proc') / str(value['pid'])).exists(), 'Uncertain descendant state during stop')
            require(time.monotonic() < deadline, 'Owned Docker descendants have not stopped; retain data')
            time.sleep(0.5)
        if capture_failure is not None:
            raise capture_failure
        records = [observed[key] for key in sorted(observed)]
        require_closed([current] + records)
        write_record(root.with_name(root.name + '.owned-process-closure.json'),
                     {'schema': 1, 'scope': scope, 'ready_sha256': ready_hash, 'daemon': current,
                      'descendants': records, 'monotonic': time.monotonic(),
                      'status': 'OBSERVED_OWNED_PROCESSES_CLOSED'})
    finally:
        os.close(handle)


def diagnostic(rows, root):
    records = []
    for row in rows:
        relative = Path(row['point']).relative_to(root)
        records.append({'mount_id': row['id'], 'parent_id': row['parent'],
                        'filesystem': row['filesystem'] if row['filesystem'] in ('overlay', 'ext4', 'xfs', 'tmpfs', 'proc', 'sysfs', 'cgroup2') else 'other',
                        'location': 'root' if not relative.parts else (relative.parts[0] if relative.parts[0] in ('overlay2', 'containers', 'containerd', 'volumes', 'image', 'buildkit') else 'other'),
                        'relative_depth': len(relative.parts),
                        'relative_path_sha256': hashlib.sha256(os.fsencode(str(relative))).hexdigest()})
    print(json.dumps({'status': 'failed', 'phase': 'private_docker_mount_teardown', 'wait_limit_seconds': 30,
                      'mount_count': len(records), 'reported_mounts': records[:16], 'truncated': len(records) > 16},
                     sort_keys=True), file=sys.stderr)

def normal_unmount(root, before, ready):
    canonical_mount_root(before['covering'])
    canonical_mount_root(ready['root_mount'])
    parentfd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        value = os.fstat(parentfd)
        require((value.st_dev, value.st_ino) == (before['parent']['device'], before['parent']['inode']),
                'Parent changed before normal unmount')
        rows = mounts()
        require(directory(root.parent) == before['parent'] and directory(root) == ready['directory'] and
                next((row for row in rows if row['id'] == before['covering']['id']), None) == before['covering'] and
                below(rows, root) == [ready['root_mount']], 'Mount changed before normal unmount')
        write_record(root.with_name(root.name + '.normal-unmount-attempt.json'),
                     {'schema': 1, 'status': 'NORMAL_UNMOUNT_ATTEMPT', 'scope': ready['scope'],
                      'namespace': namespace(), 'daemon': ready['daemon'],
                      'directory': ready['directory'], 'root_mount': ready['root_mount']})
        target = '/proc/self/fd/' + str(parentfd) + '/' + root.name
        result = subprocess.run(['/usr/bin/umount', '--no-canonicalize', '--internal-only', '--no-mtab', '--', target],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, pass_fds=(parentfd,), close_fds=True)
        require(result.returncode == 0, 'Normal owned-root unmount failed; retain data')
    finally:
        os.close(parentfd)

def cleanup(root, pidfile, scope):
    before, ready, ready_hash = load_proof(root, scope)
    empty, unused = read_record(root.with_name(root.name + '.empty-inventory.json'))
    require(empty['schema'] == 1 and empty['scope'] == scope and empty['ready_sha256'] == ready_hash and
            empty['daemon'] == ready['daemon'] and 0 <= time.monotonic() - empty['monotonic'] <= 90,
            'Fresh owned empty-container evidence required')
    empty_inventory(empty['inventory'], root)
    closed, unused = read_record(root.with_name(root.name + '.owned-process-closure.json'))
    require(closed['schema'] == 1 and closed['scope'] == scope and closed['ready_sha256'] == ready_hash and
            closed['daemon'] == ready['daemon'] and closed['status'] == 'OBSERVED_OWNED_PROCESSES_CLOSED' and
            empty['monotonic'] <= closed['monotonic'] <= empty['monotonic'] + 90,
            'Owned process closure proof differs')
    require_closed([ready['daemon']] + closed['descendants'])
    if os.path.lexists(pidfile):
        require(pidfile.read_text().strip() == str(ready['daemon']['pid']), 'Private daemon PID file differs')
    require(not (Path('/proc') / str(ready['daemon']['pid'])).exists(), 'Owned daemon has not exited')
    deadline = time.monotonic() + 30
    while True:
        rows = mounts()
        require(next((row for row in rows if row['id'] == before['covering']['id']), None) == before['covering'],
                'Backing mount changed before cleanup')
        found = below(rows, root)
        roots = [row for row in found if row['point'] == str(root)]
        descendants = [row for row in found if row['point'] != str(root)]
        if roots and roots != [ready['root_mount']]:
            diagnostic(found, root)
            raise ValueError('Unexpected root mount; retain data')
        if not descendants:
            break
        if time.monotonic() >= deadline:
            diagnostic(found, root)
            raise ValueError('Private Docker still has a mounted child; retain data')
        time.sleep(0.5)
    try:
        if roots:
            normal_unmount(root, before, ready)
        rows = mounts()
        require(not below(rows, root) and
                next((row for row in rows if row['id'] == before['covering']['id']), None) == before['covering'],
                'Mount remains or backing changed after normal unmount; retain data')
        require(directory(root.parent) == before['parent'] and directory(root) == ready['directory'],
                'Private data identity changed before deletion')
    except BaseException:
        diagnostic(below(mounts(), root), root)
        raise
    shutil.rmtree(root)

def main():
    require(os.getuid() == 0 and len(sys.argv) == 5, 'Expected elevated fixed mount action')
    os.umask(0o077)
    action, root, pidfile, scope = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), decode(sys.argv[4])
    require(action in ('before', 'ready', 'inventory', 'stop', 'cleanup') and root.is_absolute() and
            root.parent.is_relative_to('/actions/_work') and root.parent.resolve() == root.parent and
            re.fullmatch(r'source-image-docker-[1-9][0-9]*-[1-9][0-9]*', root.name) and
            scope['root'] == str(root) and scope['pidfile'] == str(pidfile), 'Unexpected owned mount scope')
    if action in ('before', 'ready'):
        capture(action, root, pidfile, scope)
    elif action == 'inventory':
        inventory(root, pidfile, scope)
    elif action == 'stop':
        stop_daemon(root, pidfile, scope)
    else:
        cleanup(root, pidfile, scope)

if __name__ == '__main__':
    main()
'''





def docker_cleanup_script():
    """Bound mount teardown before removing only the stopped private daemon's data."""
    return r'''import hashlib, json, os, pathlib, re, shutil, sys, time
root = pathlib.Path(sys.argv[1])
assert not root.is_symlink() and root.parent.resolve() == root.parent

def remaining_mounts():
    found = []
    for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        path = pathlib.Path(mount)
        if not path.is_relative_to(root):
            continue
        relative = path.relative_to(root)
        filesystem = fields[fields.index('-') + 1]
        # No mount source, options, absolute paths, or arbitrary path components.
        found.append({
            'mount_id': int(fields[0]), 'parent_id': int(fields[1]),
            'filesystem': filesystem if filesystem in ('overlay', 'ext4', 'xfs', 'tmpfs', 'proc', 'sysfs', 'cgroup2') else 'other',
            'location': ('root' if not relative.parts else relative.parts[0]
                         if relative.parts[0] in ('overlay2', 'containers', 'containerd', 'volumes', 'image', 'buildkit') else 'other'),
            'relative_depth': len(relative.parts),
            'relative_path_sha256': hashlib.sha256(os.fsencode(str(relative))).hexdigest(),
        })
    return found

deadline = time.monotonic() + 30
while True:
    mounts = remaining_mounts()
    if not mounts:
        break
    if time.monotonic() >= deadline:
        print(json.dumps({'status': 'failed', 'phase': 'private_docker_mount_teardown',
                          'wait_limit_seconds': 30, 'mount_count': len(mounts),
                          'reported_mounts': mounts[:16], 'truncated': len(mounts) > 16},
                         sort_keys=True), file=sys.stderr)
        raise AssertionError('Private Docker still has a mounted child after bounded teardown wait; retain its data')
    time.sleep(0.5)
if root.exists():
    shutil.rmtree(root)
'''


def stop(args):
    path = args.artifacts / "worker-private.json"
    if not path.exists():
        return
    state = state_for(args)
    pidfile = Path(state["pidfile"])
    if not pidfile.exists():
        require(not Path(state["docker_root"]).exists(), "Daemon left data without a PID; retain it for diagnosis")
        return
    raw = pidfile.read_text().strip()
    require(raw.isdecimal() and int(raw) > 1, "Invalid private daemon PID")
    process = Path("/proc") / raw
    if process.exists():
        require((process / "comm").read_text().strip() == "dockerd" and
                ("--data-root=" + state["docker_root"]).encode() in (process / "cmdline").read_bytes().split(b"\0"),
                "PID no longer identifies this worker's Docker daemon")
        run(["sudo", "-n", "kill", "-TERM", raw])
        deadline = time.monotonic() + 60
        while process.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        require(not process.exists(), "Private Docker has not stopped; retain its data")
    root = Path(state["docker_root"])
    temporary = temporary_directory()
    require(root.parent == temporary and temporary.is_relative_to("/actions/_work")
            and re.fullmatch(r"source-image-docker-[0-9]+-[0-9]+", root.name), "Unexpected private Docker data path")
    # Delete only this stopped job's Docker data, never its persistent Bazel caches.
    cleanup = docker_cleanup_script()
    run(["sudo", "-n", sys.executable, "-c", cleanup, str(root)], timeout=120)
    pidfile.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("selection", "kernel-preflight", "verify-input-run", "prepare", "prepare-owner", "prepare-owner-runtime", "owner-tests", "build", "validate", "incremental", "loaded", "publish", "stop", "capture-docker-mount-before", "capture-docker-mount-ready", "prepare-mirrors", "scan-mirrors", "cleanup-mirrors"))
    parser.add_argument("--image", required=True, choices=DRIVERS)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    selection(args.image, args.revision)
    input_run_id()
    args.workspace = args.workspace.resolve()
    args.artifacts = args.artifacts.resolve()
    require(not args.artifacts.is_relative_to(args.workspace) and not args.workspace.is_relative_to(args.artifacts),
            "Keep worker evidence outside the source checkout")
    if args.phase == "selection":
        return
    args.artifacts.mkdir(parents=True, exist_ok=True)
    if args.phase == "kernel-preflight":
        require(args.image == "teamd", "Kernel module preflight is only defined for Teamd")
        prepare_teamd_kernel(args.artifacts)
    elif args.phase == "verify-input-run":
        verify_input_run(args)
    elif args.phase in ("prepare-mirrors", "scan-mirrors", "cleanup-mirrors"):
        configure_worker_mirrors(args.phase, args.artifacts, image=args.image)
    elif args.phase == "prepare":
        prepare(args)
    elif args.phase == "publish":
        publish(args)
    elif args.phase == "stop":
        stop_selected(args)
    elif args.phase in ("capture-docker-mount-before", "capture-docker-mount-ready"):
        capture_docker_mount(args, args.phase.rsplit("-", 1)[1])
    else:
        execute_phase(args)


if __name__ == "__main__":
    main()
