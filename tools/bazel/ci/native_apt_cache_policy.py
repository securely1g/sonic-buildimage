"""Inspect the installed optional native APT cache without changing or probing it."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
from urllib.parse import urlsplit


MOUNT = Path("/run/sonic-download-proxy")
STATE = Path("/run/sonic-native-apt")
CONFIG = Path("/etc/apt/apt.conf.d/99sonic-native-squid")
SOURCE_DIR = Path("/etc/apt/sources.list.d")
SOURCE = SOURCE_DIR / "debian.sources"
KEYRING = Path("/usr/share/keyrings/debian-archive-keyring.pgp")
KEYRING_SHA = "506b815cbb32d9b6066b4a2aa524071e071761e7e7f68c3ac74f3061ba852017"
HELPER_SHA = "bb089e8c66b5e501308390f0edfb139fef079d6afb9ae6769590d2d4d4168a7c"
HOOK_SHA = "e1a0b2becd466697bc53c47adff8425f3f9912aafcd26f2595343a90d918d4d3"
ENDPOINT_SHA = "d8b3d50904b53b8c671cd38a811ba917cd765c7b9bf32e0599e330b2220ded6e"
HELPER = "/run/sonic-download-proxy/native_apt_proxy.py"
PROXY_KEYS = tuple("Acquire::" + scheme + "::" + key for scheme in ("http", "https")
                   for key in ("Proxy", "Proxy::deb.debian.org", "Proxy-Auto-Detect", "ProxyAutoDetect"))
TRUST_KEYS = ("APT::Get::AllowUnauthenticated", "Acquire::AllowInsecureRepositories",
              "Acquire::AllowDowngradeToInsecureRepositories", "Acquire::AllowWeakRepositories")
CONFIG_KEYS = PROXY_KEYS + tuple("Binary::" + method + "::" + key
    for method in ("http", "https") for key in PROXY_KEYS) + TRUST_KEYS + (
    "Dir", "Dir::Etc", "Dir::Etc::sourcelist", "Dir::Etc::sourceparts")
DIRECT_OPTIONS = ("-o", "Acquire::http::Proxy::deb.debian.org=DIRECT",
                  "-o", "Acquire::https::Proxy::deb.debian.org=DIRECT")


def require(value):
    if not value:
        raise ValueError("Owned optional APT cache policy differs")


def proxy_environment_absent(environ):
    return "APT_CONFIG" not in environ and not any(
        name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"} for name in environ)


def file_identity(info):
    return tuple(getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid",
                 "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))


def protected_directory(path):
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
            (not info.st_mode & 0o022 or os.statvfs(path).f_flag & os.ST_RDONLY))


def read_public_file(path, root, limit):
    """Hold a bounded public file; follow projected links only inside its fixed root."""
    require(root.resolve(strict=True) == root)
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(root))
    parent = resolved.parent
    while True:
        protected_directory(parent)
        if parent == root:
            break
        parent = parent.parent
    with os.fdopen(os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        before = os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and
                not before.st_mode & 0o022 and 0 <= before.st_size <= limit)
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        require(file_identity(before) == file_identity(after) and len(data) == before.st_size and
                path.resolve(strict=True) == resolved and file_identity(resolved.stat()) == file_identity(after))
    return data, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "uid": before.st_uid, "mode": stat.S_IMODE(before.st_mode)}


def verified_cache_address():
    """Keep the exact reviewed endpoint private; never resolve an arbitrary host."""
    data, info = read_public_file(MOUNT / "endpoint", MOUNT, 256)
    require(info["sha256"] == ENDPOINT_SHA == hashlib.sha256(data).hexdigest())
    parsed = urlsplit(data.decode("ascii"))
    require(parsed.scheme == "http" and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment
            and parsed.port is not None and 1 <= parsed.port <= 65535)
    address = ipaddress.IPv4Address(parsed.hostname)
    return str(address), parsed.port


def expected_policy(data):
    prefix = ("Acquire::http::Proxy-Auto-Detect \"" + HELPER + "\";\n"
              "Acquire::https::Proxy-Auto-Detect \"" + HELPER + "\";\n")
    return data in {(prefix + "Acquire::https::deb.debian.org::CAInfo \"/run/sonic-native-apt/" +
                    name + "\";\n").encode() for name in ("ca-bundle.pem", "apt-ca-bundle.pem")}


def official_sources(data):
    """Admit the stock Debian Deb822 source shape; never rewrite URLs or trust."""
    paragraphs = []
    current = {}
    for line in data.decode("utf-8").splitlines() + [""]:
        if line.startswith("#"):
            continue
        if not line.strip():
            if current:
                paragraphs.append(current)
                current = {}
            continue
        require(not line[0].isspace())
        key, colon, value = line.partition(":")
        require(colon and key not in current)
        current[key] = value.strip()
    require(0 < len(paragraphs) <= 4)
    for row in paragraphs:
        require(set(row) == {"Types", "URIs", "Suites", "Components", "Signed-By"})
        require(row["Types"] == "deb" and row["Signed-By"] == str(KEYRING))
        require(set(row["Suites"].split()) and set(row["Suites"].split()) <=
                {"trixie", "trixie-updates", "trixie-security"})
        require(set(row["Components"].split()) and set(row["Components"].split()) <=
                {"main", "contrib", "non-free", "non-free-firmware"})
        require(0 < len(row["URIs"].split()) <= 2)
        for value in row["URIs"].split():
            uri = urlsplit(value)
            require(uri.scheme in {"http", "https"} and uri.hostname == "deb.debian.org" and
                    uri.username is None and uri.password is None and
                    uri.port in (None, 443 if uri.scheme == "https" else 80) and
                    uri.path.rstrip("/") in {"/debian", "/debian-security"} and
                    not uri.query and not uri.fragment)
    return True


def configuration():
    args = ["apt-get", "shell"]
    for index, key in enumerate(CONFIG_KEYS):
        args.extend(("K" + str(index), key))
    # Match the existing APT diagnostic: argv[0] selects apt-get's Binary scope.
    result = subprocess.run(args, executable="/usr/bin/apt-config", check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    require(len(result.stdout) <= 65536)
    values = {}
    for item in shlex.split(result.stdout):
        key, separator, value = item.partition("=")
        require(separator and key in {"K" + str(i) for i in range(len(CONFIG_KEYS))} and key not in values)
        values[key] = value
    return {key: values["K" + str(i)] for i, key in enumerate(CONFIG_KEYS) if "K" + str(i) in values}


def configuration_matches(values):
    expected = {"Acquire::" + scheme + "::Proxy-Auto-Detect": HELPER for scheme in ("http", "https")}
    if {key: values[key] for key in values if key in CONFIG_KEYS[:24]} != expected:
        return False
    if any(values.get(key, "false").lower() not in {"false", "0", "no"} for key in TRUST_KEYS):
        return False
    defaults = {"Dir": "/", "Dir::Etc": "etc/apt", "Dir::Etc::sourcelist": "sources.list",
                "Dir::Etc::sourceparts": "sources.list.d"}
    return all(values.get(key, default) == default for key, default in defaults.items())


def inspect():
    """Return only public identities and booleans, with no network or file mutation."""
    result = {"schema": 1, "status": "not_eligible", "scope": "owned_optional_apt_cache_debian_source_only",
              "uid": os.getuid(), "files": {}, "proxy_environment_absent": proxy_environment_absent(os.environ),
              "stage": "uid_and_environment"}
    try:
        require(result["uid"] == 0 and result["proxy_environment_absent"])
        selected = (("helper", MOUNT / "native_apt_proxy.py", MOUNT, 4096),
                    ("hook", MOUNT / "proxy-job-started.sh", MOUNT, 8192),
                    ("endpoint", MOUNT / "endpoint", MOUNT, 256),
                    ("policy", STATE / "policy", STATE, 4096),
                    ("marker", STATE / ".native-proxy-owned", STATE, 0),
                    ("sources", SOURCE, SOURCE_DIR, 16384),
                    ("keyring", KEYRING, KEYRING.parent, 2 * 1024 * 1024))
        contents = {}
        for name, path, root, limit in selected:
            result["stage"] = "read_" + name
            contents[name], result["files"][name] = read_public_file(path, root, limit)
        result["stage"] = "owner_identity"
        result["helper_matches"] = result["files"]["helper"]["sha256"] == HELPER_SHA
        result["hook_matches"] = result["files"]["hook"]["sha256"] == HOOK_SHA
        result["endpoint_matches"] = result["files"]["endpoint"]["sha256"] == ENDPOINT_SHA
        result["keyring_matches"] = result["files"]["keyring"]["sha256"] == KEYRING_SHA
        require(CONFIG.parent.resolve(strict=True) == CONFIG.parent)
        protected_directory(CONFIG.parent)
        link = CONFIG.lstat()
        result["owned_policy_link"] = stat.S_ISLNK(link.st_mode) and link.st_uid == 0 and os.readlink(CONFIG) == str(STATE / "policy")
        result["policy_matches"] = expected_policy(contents["policy"])
        require(all(result[key] for key in ("helper_matches", "hook_matches", "endpoint_matches",
                                            "owned_policy_link", "policy_matches", "keyring_matches")))
        result["stage"] = "official_signed_sources"
        # The direct invocation selects only this verified file and a fresh empty
        # sourceparts directory. Other configured repositories stay untouched.
        result["official_signed_sources"] = official_sources(contents["sources"])
        result["stage"] = "apt_configuration"
        result["configuration_matches"] = configuration_matches(configuration())
        require(result["configuration_matches"])
        result["status"] = "eligible"
        result["stage"] = "complete"
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        result["error_class"] = type(error).__name__
    return result


def validate_report(value):
    booleans = {"proxy_environment_absent", "helper_matches", "hook_matches", "endpoint_matches",
                "owned_policy_link", "policy_matches", "keyring_matches", "official_signed_sources", "configuration_matches"}
    stages = {"uid_and_environment", "owner_identity", "official_signed_sources", "apt_configuration", "complete"}
    roles = {"helper", "hook", "endpoint", "policy", "marker", "sources", "keyring"}
    stages |= {"read_" + name for name in roles}
    require(type(value) is dict and set(value) <= booleans | {
        "schema", "status", "scope", "uid", "files", "stage", "error_class"})
    require(value["schema"] == 1 and value["status"] in {"eligible", "not_eligible"} and
            value["scope"] == "owned_optional_apt_cache_debian_source_only" and value["uid"] == 0 and
            value["stage"] in stages and type(value["files"]) is dict and set(value["files"]) <= roles)
    require(all(type(value[key]) is bool for key in booleans if key in value))
    for row in value["files"].values():
        require(type(row) is dict and set(row) == {"bytes", "sha256", "uid", "mode"} and
                type(row["bytes"]) is int and 0 <= row["bytes"] <= 2 * 1024 * 1024 and row["uid"] == 0 and
                type(row["mode"]) is int and 0 <= row["mode"] <= 0o7777 and
                type(row["sha256"]) is str and re.fullmatch("[0-9a-f]{64}", row["sha256"]))
    if "error_class" in value:
        require(type(value["error_class"]) is str and re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,63}", value["error_class"]))
    if value["status"] == "eligible":
        require(value["stage"] == "complete" and "error_class" not in value and
                all(value.get(key) is True for key in booleans) and roles <= set(value["files"]))
    return value


if __name__ == "__main__":
    print(json.dumps(validate_report(inspect()), sort_keys=True))
