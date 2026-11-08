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
from tools.bazel.ci.public_artifacts import safe_json, dependency_json, bep_summary, load_json
from tools.bazel.ci.source_worker_preflight import cpu_limit, measure, select_build_profile
from tools.bazel.gzip.source_archive import SOURCES

DRIVERS = {"dash-ha": "dash_ha", "sysmgr": "sysmgr", "orchagent": "orchagent", "sflow": "sflow", "otel": "otel", "syncd": "syncd", "gearbox": "gbsyncd", "dash-engine": "dash_engine", "gnmi": "gnmi", "gnmi-no-dialout": "gnmi", "framework": "mgmt_framework", "teamd": "teamd",
           "database": "database", "eventd": "eventd", "lldp": "lldp",
           "radv": "radv", "macsec": "macsec", "mux": "mux", "frr": "frr", "snmp": "snmp", "p4rt": "p4rt",
           "base-config": "base", "swss-layer": "swss_layer"}
SHARED_PROFILES = {"base-config", "swss-layer"}
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
MIRROR_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/3a7e32d09e511735f36a47b276d6524cf9c2a5c4/ci/source_mirrors.cfg"
MIRROR_SHA = "38192a1561fac945a22474f1f10f29397d3164974e6f6b1e71de7c4748088f96"


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


def selection(image, revision):
    require(image in DRIVERS, "Unknown source image")
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


def prepare_validation_tools(artifacts):
    """Install only missing execution tools in the disposable slave container."""
    packages = {"bsdtar": "libarchive-tools", "gdb": "gdb", "readelf": "binutils", "objcopy": "binutils"}
    missing = sorted({package for tool, package in packages.items() if shutil.which(tool) is None})
    if missing:
        try:
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


def launcher_text(binary, base, profile):
    flags = ["--remote_cache=", "--remote_download_regex=(^|.*/)tools/build_tools/kernel_runtime_(amd64|arm64)(/.*)?$",
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


def owner_launcher_text(binary, base, rc, cache):
    # Keep the WORKSPACE owner's Bazel6 options separate from the Bazel8 launcher.
    return ("#!/usr/bin/env python3\nimport os, sys\n" +
            "os.environ['USE_BAZEL_VERSION'] = '6.4.0'\n" +
            "os.environ['BAZELISK_HOME'] = " + repr(str(cache / "bazelisk")) + "\n" +
            "binary = " + repr(binary) + "\n" +
            "os.execv(binary, [binary, " + repr("--output_base=" + str(base)) + ", " +
            "'--host_jvm_args=-Xmx2g', " +
            "'--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts', " +
            "'--nosystem_rc', '--nohome_rc', " + repr("--bazelrc=" + str(rc)) + ", *sys.argv[1:]])\n")


def prepare_p4rt(workspace, cache, temporary, distdirs, mirror):
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
    for name in ("repository", "disk", "bazelisk", "output-base"):
        path = owner_cache / name
        require(path.resolve() == path, "P4RT owner cache child must not traverse symlinks")
        path.mkdir(parents=True, exist_ok=True)
    rc = temporary / "p4rt-owner.bazelrc"
    launch = temporary / "p4rt-owner-bazel"
    require(not rc.exists() and not launch.exists(), "P4RT owner launcher must be fresh")
    rc.write_text("\n".join([
        "common --repository_cache=" + str(owner_cache / "repository"),
        "build --disk_cache=" + str(owner_cache / "disk"),
        "build --local_ram_resources=4096",
        "common --experimental_downloader_config=" + str(mirror),
        *["common --distdir=" + str(directory) for directory in distdirs],
    ]) + "\n")
    base = owner_cache / "output-base"
    launch.write_text(owner_launcher_text(shutil.which("bazel"), base, rc, owner_cache))
    launch.chmod(0o755)
    return {"source": str(owner), "revision": P4RT_OWNER, "bazel": str(launch), "bazel_sha256": digest(launch),
            "rc": str(rc), "rc_sha256": digest(rc), "output_base": str(base), "jobs": 1}


def p4rt_context(args, state):
    context = state.get("p4rt_owner", {})
    owner = p4rt_owner_identity(args.workspace)
    cache = Path("/bazel_cache/source-images") / os.environ.get("RUNNER_NAME", "") / "p4rt-owner"
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
    if image in SHARED_PROFILES:
        require(phase in ("build-inspect", "incremental", "loaded"), "Unknown shared-image phase")
        return [sys.executable, "-B", str(ROOT / "tools/bazel/ci/shared_image_worker.py"), phase,
                "--image", image, "--workspace", str(workspace), "--artifacts", str(artifacts)]
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
            re.fullmatch(r"sonic-slave-lgh114-[1-4]", runner), "Invalid worker scratch identity")
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
    mirrors = private_mirrors.selected_mirrors(roots)
    if mirrors:
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and
                os.environ.get("GITHUB_REPOSITORY") == "securely1g/sonic-buildimage" and
                os.environ.get("GITHUB_ACTOR") == "securely1g" and
                platform.machine() == "x86_64" and
                platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie" and
                re.fullmatch(r"sonic-slave-lgh114-[1-4]", os.environ.get("RUNNER_NAME", "")),
                "Private Debian mirrors require a trusted native Trixie worker")
    public_config = ROOT / "bazel-public-inputs/ci/source_mirrors.cfg"
    public = mirror_configuration(public_config.read_bytes()).decode()
    private_mirrors.write_config(config, private_mirrors.configuration(public, mirrors) if mirrors else public)


def prepare(args):
    github_token = os.environ.pop("GITHUB_TOKEN", "")
    workspace, artifacts = args.workspace, args.artifacts
    if args.image == "teamd":
        prepare_teamd_kernel(artifacts)
    preflight = measure(os.environ.get("RUNNER_NAME", ""))
    (artifacts / "worker-preflight.json").write_text(json.dumps(preflight, indent=2) + "\n")
    require(preflight["status"] == "passed", "Worker resource/capability preflight failed")
    profile = preflight["build_profile"]
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
    for mount in ("/actions", "/bazel_cache"):
        run(["mountpoint", "-q", mount])
    require(output(["findmnt", "-n", "-o", "FSTYPE", "-T", "/actions"]) in ("ext4", "xfs"), "Docker storage requires ext4 or XFS")
    if args.image == "syncd":
        prepare_tap_fixture(artifacts)
    prepare_validation_tools(artifacts)
    source_check(workspace, args.revision)
    if args.image in SHARED_PROFILES:
        from tools.bazel.ci.shared_image_worker import required_source_files
        for name in required_source_files(args.image):
            path = workspace / name
            require(path.is_file() and path.resolve().is_relative_to(workspace), "Missing reviewed shared-image input: " + name)
        driver_names = ()
    else:
        driver_names = (f"{DRIVERS[args.image]}_image.py", f"{DRIVERS[args.image]}_loaded.py")
    for name in (*driver_names, WRAPPERS[args.image], "review_bazel.sh"):
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
    cache = Path("/bazel_cache/source-images") / runner
    require(cache.resolve() == cache, "Cache path must not traverse symlinks")
    for name in ("repository", "disk", "bazelisk", args.image + "/output-base"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    temporary = temporary_directory()
    require(temporary.is_relative_to("/actions/_work"), "Expected a worker-private temporary directory")
    temporary.mkdir(parents=True, exist_ok=True)
    distdirs, download_receipt = prepare_source_downloads(temporary / "source-image-inputs", cache)
    (artifacts / "worker-source-downloads.json").write_text(json.dumps(download_receipt, indent=2) + "\n")
    public_inputs = ROOT / "bazel-public-inputs/ci"
    rule_distdir = cache / "rule-distdir"
    rule_receipt = temporary / "rule-sources.json"
    run([sys.executable, "-B", str(public_inputs / "github_sources.py"),
         "--manifest", str(public_inputs / "github_sources/sonic-buildimage-rule-inputs.json"),
         "--distdir", str(rule_distdir), "--repository-cache", str(cache / "repository"),
         "--cache-dir", str(cache / "github-sources"), "--receipt", str(rule_receipt),
         "--github-token-env", "GITHUB_TOKEN"], env={**os.environ, "GITHUB_TOKEN": github_token})
    rule_sources = load_json(rule_receipt.read_text())
    safe_json(rule_sources)
    distdirs.append(rule_distdir)
    launch = temporary / "source-image-bazel"
    launch.write_text(launcher_text(shutil.which("bazel"), cache / args.image / "output-base", profile))
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
        "# Private persistent source-image worker caches; no Make package cache.",
        "common --repository_cache=" + str(cache / "repository"),
        "common --disk_cache=" + str(cache / "disk"),
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
    if args.image == "p4rt":
        state["p4rt_owner"] = prepare_p4rt(workspace, cache, temporary, distdirs, mirror)
        env["USE_BAZEL_VERSION"] = "8.5.1"
    (artifacts / "worker-private.json").write_text(json.dumps(state, indent=2) + "\n")
    run(["sudo", "-n", sys.executable, str(ROOT / "tools/bazel/ci/prepare_nested_docker.py")])
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
    receipt = {"schema": 1, "status": "prepared", "worker": runner, "architecture": "amd64", "distribution": "trixie",
               "source_revision": args.revision, "dispatcher_revision": output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
               "review_catalog": catalog, "slave_image": marker_data["base_image"], "cache_mode": "persistent_worker_local",
               "build_profile": profile, "memory": memory_snapshot(),
               "docker_network": "none", "image": args.image, "rule_sources": rule_sources}
    if args.image == "p4rt":
        receipt["owner_profile"] = P4RT_OWNER_PROFILE
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
        receipt["owner_profile"] = P4RT_OWNER_PROFILE
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
    shared = getattr(args, "image", None) in SHARED_PROFILES
    receipt_path = source / ("receipt.json" if shared else "build-receipt.json")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if getattr(args, "image", None) in GNMI_PROFILES:
            require(receipt.get("dialout_enabled") is (GNMI_PROFILES[args.image] == "enabled"),
                    "gNMI receipt differs from the selected fixed dialout profile")
        public = {k: receipt[k] for k in ("status", "architecture", "revision", "tested_revision_parents", "dialout_enabled", "artifacts") if k in receipt}
        if shared and "installed_images_policy_sha256" in receipt:
            public["installed_images_policy_sha256"] = receipt["installed_images_policy_sha256"]
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
    if shared:
        names += ["installed-images-policy.json", "installed-images.json", "runtime-smoke.json",
                  "config-engine-rebuild.json", "consumer-action-audit.json"]
    names += [p.name for pattern in ("worker-*.json", "*.validation.json", "*.runtime.json") for p in source.glob(pattern)
              if p.name != "worker-private.json"]
    for name in sorted(set(names)):
        path = source / name
        if path.is_file():
            value = json.loads(path.read_text())
            if getattr(args, "image", None) == "snmp" and name in (
                    "docker-snmp.validation.json", "docker-snmp-dbg.validation.json"):
                # This is the fixed public OCI configuration, not a captured
                # process environment. Check its exact constants only in the
                # safety-validation view; preserve the original loaded receipt.
                require(isinstance(value.get("config"), dict) and value["config"].pop("Env", None) == [
                    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "DEBIAN_FRONTEND=noninteractive", "PYTHONOPTIMIZE=1", "IMAGE_VERSION=bazel-snmp-ci",
                ], "Unexpected SNMP image environment")
            projected = None
            if getattr(args, "image", None) == "base-config":
                from tools.bazel.ci.shared_image_worker import public_view
                projected = public_view(name, value, digest(path))
            safe_json(projected if projected is not None else value)
            target = destination / name
            target.parent.mkdir(exist_ok=True)
            if projected is None:
                shutil.copyfile(path, target)
            else:
                target.write_text(json.dumps(projected, indent=2) + "\n")
    for name, kind in (("MODULE.bazel.lock", "lock"), ("module-graph.json", "graph")):
        path = source / name
        if path.is_file():
            dependency_json(json.loads(path.read_text()), kind)
            shutil.copyfile(path, destination / name)
    summary = {str(path.relative_to(source)): bep_summary(path.read_text().splitlines(), partial=True)
               for path in source.rglob("*events.json*") if not path.is_relative_to(destination)}
    safe_json(summary)
    (destination / "build-summary.json").write_text(json.dumps(summary, indent=2) + "\n")


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
    cleanup = """import os, pathlib, re, shutil, sys
root = pathlib.Path(sys.argv[1])
assert not root.is_symlink() and root.parent.resolve() == root.parent
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
    mount = re.sub(r'\\\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4])
    assert not pathlib.Path(mount).is_relative_to(root), 'Private Docker still has a mounted child'
if root.exists():
    shutil.rmtree(root)
"""
    run(["sudo", "-n", sys.executable, "-c", cleanup, str(root)], timeout=120)
    pidfile.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("selection", "kernel-preflight", "verify-input-run", "prepare", "prepare-owner", "prepare-owner-runtime", "owner-tests", "build", "build-inspect", "validate", "incremental", "loaded", "publish", "stop", "prepare-mirrors", "scan-mirrors", "cleanup-mirrors"))
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
        stop(args)
    else:
        execute_phase(args)


if __name__ == "__main__":
    main()
