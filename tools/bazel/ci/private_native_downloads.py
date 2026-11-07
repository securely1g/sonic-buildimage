#!/usr/bin/env python3
"""Use the existing private Debian mirror policy for one native Make command.

APT keeps its public sources and signed metadata. Its existing HTTP proxy hook
reaches this disposable adapter, which fetches Debian files over verified HTTPS.
No private origin is copied into a Docker layer or the installer filesystem.
"""

import argparse
import http.server
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

REGISTRY_REVISION = "1060b4084e0598312c446eb5bb66be938aeac22c"
DEBIAN_HOSTS = {"deb.debian.org", "security.debian.org", "ftp.debian.org"}
SNAPSHOT_HOSTS = {"snapshot.debian.org", "snapshot-cloudflare.debian.org"}
BRIDGE = "192.168.240.1"
BRIDGE_NETWORK = ipaddress.ip_network("192.168.240.0/24")


def public_apt_transport(root):
    """Keep public sources while routing this disposable worker through the proxy.

    The native rootfs and Docker recipes already render HTTP public sources.
    The immutable worker image can also contain HTTPS snapshot sources; switch
    only those known public Debian URIs to the local proxy's HTTP transport.
    The proxy itself always verifies TLS to the private upstream.
    """
    pattern = re.compile(r"https://(" + "|".join(re.escape(host) for host in
                         sorted(DEBIAN_HOSTS | SNAPSHOT_HOSTS)) + r")(?=/)")
    paths = [root / "sources.list", *sorted((root / "sources.list.d").glob("*.list")),
             *sorted((root / "sources.list.d").glob("*.sources"))]
    changed = 0
    for path in paths:
        if path.is_symlink():
            raise ValueError("APT sources must be regular files")
        if not path.exists():
            continue
        original = path.read_text()
        selected = pattern.sub(r"http://\1", original)
        if selected != original:
            path.write_text(selected)
            changed += 1
    return changed


def destination(uri, mirrors, manifest):
    parsed = urllib.parse.urlsplit(uri)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.port not in (None, 80)
            or any(character.isspace() for character in uri)):
        raise ValueError("Unsupported native HTTP request")
    if parsed.hostname in DEBIAN_HOSTS:
        if parsed.query or ".." in urllib.parse.unquote(parsed.path).split("/"):
            raise ValueError("Unsupported Debian archive path")
        archive, separator, suffix = parsed.path.lstrip("/").partition("/")
        if not separator or archive not in {"debian", "debian-security"}:
            raise ValueError("Unsupported Debian archive path")
        key = "DEBIAN_MIRROR_ROOT" if archive == "debian" else "DEBIAN_SECURITY_MIRROR_ROOT"
        return mirrors[key] + manifest[archive]["current"] + "/" + suffix, archive
    if parsed.hostname in SNAPSHOT_HOSTS:
        if parsed.query or ".." in urllib.parse.unquote(parsed.path).split("/"):
            raise ValueError("Unsupported Debian snapshot path")
        parts = parsed.path.lstrip("/").split("/", 3)
        if (len(parts) != 4 or parts[0] != "archive" or parts[1] not in manifest
                or parts[2] not in manifest[parts[1]]["snapshots"]):
            raise ValueError("Unsupported Debian snapshot")
        archive = parts[1]
        key = "DEBIAN_MIRROR_ROOT" if archive == "debian" else "DEBIAN_SECURITY_MIRROR_ROOT"
        return mirrors[key] + parts[2] + "/" + parts[3], archive
    # Existing Make recipes also fetch unrelated public HTTP resources. Leave
    # those requests unchanged; this adapter adds no cache and no credentials.
    return uri, None


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", None, None)


def handler(mirrors, manifest, counters, *, opener=None):
    private_opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    public_opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    counter_lock = threading.Lock()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request/error strings can contain a private upstream URL.

        def do_CONNECT(self):
            self.send_error(405, "HTTPS tunnelling is not supported")

        def do_HEAD(self):
            self.transfer(head=True)

        def do_GET(self):
            self.transfer(head=False)

        def transfer(self, *, head):
            source = ipaddress.ip_address(self.client_address[0])
            if not (source.is_loopback or source in BRIDGE_NETWORK):
                self.send_error(403, "Only the private build network is allowed")
                return
            try:
                uri, archive = destination(self.path, mirrors, manifest)
                request = urllib.request.Request(uri, method="HEAD" if head else "GET")
                # APT may resume a download. Never forward client credentials,
                # cookies, proxy headers, or a private redirect Location.
                for name in ("Range", "If-Modified-Since", "If-None-Match"):
                    if self.headers.get(name):
                        request.add_header(name, self.headers[name])
                selected = private_opener if archive else public_opener
                with selected.open(request, timeout=60) as response:
                    if archive:
                        with counter_lock:
                            counters[archive] += 1
                    self.send_response(response.status)
                    for name in ("Content-Length", "Content-Type", "Content-Range", "Last-Modified", "ETag"):
                        if response.headers.get(name):
                            self.send_header(name, response.headers[name])
                    self.end_headers()
                    if not head:
                        shutil.copyfileobj(response, self.wfile, length=1024 * 1024)
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    if archive:
                        with counter_lock:
                            counters[archive] += 1
                    self.send_response(304)
                    for name in ("Last-Modified", "ETag"):
                        if error.headers and error.headers.get(name):
                            self.send_header(name, error.headers[name])
                    self.end_headers()
                    error.close()
                    return
                code = error.code if error.code in {404, 410, 416} else 502
                error.close()
                self.send_error(code, "Upstream download unavailable")
            except (OSError, ValueError, urllib.error.URLError):
                self.send_error(502, "Upstream download unavailable")

    return Proxy


def bazel_command(arguments, config, real_bazel):
    commands = {"build", "test", "run", "fetch", "query", "cquery", "aquery", "info", "mod", "sync"}
    command = next((argument for argument in arguments
                    if argument in commands | {"version", "help", "shutdown", "clean"}), None)
    if command in commands:
        end = arguments.index("--") if "--" in arguments else len(arguments)
        arguments = [*arguments[:end], "--experimental_downloader_config=" + str(config),
                     "--disk_cache=", "--repository_cache=", *arguments[end:]]
    return [real_bazel, *arguments]


def alternatives_metadata(payload):
    """Recognize the text state written by dpkg's update-alternatives."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.endswith("\n") or any(ord(char) < 32 and char != "\n" for char in text):
        return False
    lines = text.split("\n")[:-1]
    if len(lines) < 5 or lines[0] not in {"auto", "manual"} or not lines[1].startswith("/"):
        return False
    try:
        end = lines.index("", 2)
    except ValueError:
        return False
    if (end - 2) % 2:
        return False
    for index in range(2, end, 2):
        if not lines[index] or "/" in lines[index] or not lines[index + 1].startswith("/"):
            return False
    slaves = (end - 2) // 2
    index, choices = end + 1, 0
    while index < len(lines):
        stop = index + 2 + slaves
        if (stop > len(lines) or not lines[index].startswith("/")
                or not re.fullmatch(r"[+-]?[0-9]+", lines[index + 1])
                or any(value and not value.startswith("/") for value in lines[index + 2:stop])):
            return False
        choices += 1
        index = stop
        if index < len(lines):
            if lines[index] != "":
                return False
            index += 1
    return choices > 0


def native_redactor(base):
    """Scan alternatives state as text even when its link name ends in .gz."""
    class NativeRedactor(base):
        def _scan_stream(self, stream, name="", depth=0):
            parts = PurePosixPath(name).parts
            if len(parts) == 5 and parts[:4] == ("var", "lib", "dpkg", "alternatives"):
                payload = stream.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise ValueError("Oversized dpkg alternatives metadata")
                stream = io.BytesIO(payload)
                if alternatives_metadata(payload):
                    # Keep the payload unchanged and scan every byte. The empty
                    # name disables only the misleading archive suffix check;
                    # genuine archives still use the pinned scanner below.
                    name = ""
            return super()._scan_stream(stream, name, depth)
    return NativeRedactor


def run(args):
    # The workflow checks out this exact first-parent main revision. Import its
    # reviewed validation, rewrite, and privacy code instead of inventing roots.
    helpers = args.helpers.resolve(strict=True)
    revision = subprocess.check_output(["git", "-C", str(helpers.parent), "rev-parse", "HEAD"], text=True).strip()
    if revision != REGISTRY_REVISION:
        raise ValueError("Private download helper revision differs from the workflow pin")
    sys.path.insert(0, str(helpers))
    import private_mirrors
    from mirror_privacy import Redactor
    mirrors = private_mirrors.selected_mirrors(dict(os.environ))
    if set(mirrors) != {"DEBIAN_MIRROR_ROOT", "DEBIAN_SECURITY_MIRROR_ROOT"}:
        raise ValueError("Both private Debian mirror roots are required")
    if not os.environ.get("SONIC_BAZEL_REMOTE_ONLY_RC"):
        raise ValueError("Managed remote cache options are required")
    remote_rc = Path(os.environ["SONIC_BAZEL_REMOTE_ONLY_RC"]).resolve(strict=True)
    remote_origins = [line.split("=", 1)[1] for line in remote_rc.read_text().splitlines()
                      if line.startswith("common --remote_cache=https://")]
    if len(remote_origins) != 1:
        raise ValueError("Host-selected remote cache configuration is required")
    redactor = native_redactor(Redactor)([*mirrors.values(), *remote_origins])
    redactor.mask_github()
    manifest = private_mirrors.load_manifest()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    if args.check_artifacts:
        redactor.scrub_and_scan(artifacts)
        # These are the container archives selected by this job for publication.
        # The reviewed scanner inspects compressed layers without extracting
        # device nodes, following archive symlinks, or executing image content.
        for name in ("docker-orchagent.gz", "docker-orchagent-dbg.gz",
                     "docker-syncd-vs.gz", "docker-syncd-vs-dbg.gz"):
            path = Path("target") / name
            if path.is_file():
                with path.open("rb") as contents:
                    redactor._scan_stream(contents, path.name)
        print("Private download evidence check passed")
        return 0
    args.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.private_dir.resolve().is_relative_to(artifacts):
        raise ValueError("Private download configuration must be outside artifacts")
    counts = {"debian": 0, "debian-security": 0}
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("Missing native command")
    with tempfile.TemporaryDirectory(prefix="native-", dir=args.private_dir) as temporary:
        private = Path(temporary)
        config = private / "downloader.cfg"
        # Native input resolution must try the next signed-index variant
        # promptly when the mirror does not publish a compression format.
        # Public mirror connection retries delay those expected misses here.
        public_fallback = args.phase != "inputs"
        private_mirrors.write_config(config, private_mirrors.configuration(
            Path("tools/bazel/debian_mirrors.cfg").read_text(), mirrors,
            fallback=public_fallback, manifest=manifest))
        apt_map = private / "apt-mirrors.json"
        private_mirrors.write_config(apt_map, json.dumps({"schema_version": 1,
            "mirrors": private_mirrors.apt_mirror_map(mirrors, manifest)}))
        real_bazel = shutil.which(os.environ.get("BAZEL", "bazel"))
        if not real_bazel:
            raise ValueError("Bazel launcher is unavailable")
        launcher = private / "bazel"
        launcher.write_text("#!/bin/sh\nexec " + shlex.join([
            sys.executable, "-B", str(Path(__file__).resolve()), "bazel", str(config), real_bazel]) + ' "$@"\n')
        launcher.chmod(0o700)
        inherited = private_mirrors.child_environment()
        # APT's HTTPS method otherwise inherits http_proxy and sends CONNECT
        # to this HTTP-only adapter. Preserve an explicit HTTPS proxy; an
        # explicit empty value keeps unrelated HTTPS downloads direct.
        https_proxy = inherited.get("https_proxy", inherited.get("HTTPS_PROXY", ""))
        environment = inherited | {
            "BAZEL": str(launcher), "SONIC_BAZEL_DOWNLOADER_CONFIG": str(config),
            "SONIC_BAZEL_REMOTE_ONLY_RC": str(remote_rc),
            "SONIC_APT_MIRROR_CONFIG": str(apt_map),
            "SONIC_NATIVE_PRIVATE_APT": "1",
            "BAZEL_CONTAINER_CACHE_DIR": "", "BAZEL_SWSS_CACHE_DIR": "",
        }
        server = thread = None
        if args.phase != "inputs":
            # The input producer only resolves Bazel repositories. It has no
            # Docker bridge yet and needs the private downloader, not an APT proxy.
            server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler(mirrors, manifest, counts))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            proxy = f"http://{BRIDGE}:{server.server_port}"
            environment.update(http_proxy=proxy, HTTP_PROXY=proxy,
                               https_proxy=https_proxy, HTTPS_PROXY=https_proxy)
        try:
            with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace") as child:
                for line in child.stdout:
                    print(redactor.redact(line), end="", flush=True)
                result = child.wait()
                if result == 0 and args.phase == "build" and not all(counts.values()):
                    raise ValueError("Native build did not prove both private APT archives")
                return result
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            receipt = {"schema": 1, "helper_revision": REGISTRY_REVISION,
                       "private_mirrors_enabled": True, "native_apt_upstream_https": server is not None,
                       "public_bazel_fallback": public_fallback,
                       "native_apt_upstream_responses": counts, "package_integrity_unchanged": True,
                       "public_apt_sources_preserved": True, "local_bazel_caches": False}
            (artifacts / ("private-downloads-" + args.phase + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
            redactor.scrub_and_scan(artifacts)


def main():
    if sys.argv[1:] == ["public-apt-transport"]:
        if os.geteuid() != 0 or not Path("/.dockerenv").is_file():
            raise SystemExit("Public APT transport preparation requires the disposable native worker")
        print(json.dumps({"public_sources_preserved": True,
                          "transport_files_changed": public_apt_transport(Path("/etc/apt"))}))
        return 0
    if sys.argv[1:2] == ["bazel"]:
        os.execv(sys.argv[3], bazel_command(sys.argv[4:], Path(sys.argv[2]), sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helpers", type=Path, default=Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "private-downloads/ci")
    parser.add_argument("--private-dir", type=Path, default=Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "native-downloads")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/vs"))
    parser.add_argument("--phase", required=True, choices=("inputs", "preparation", "build", "verification"))
    parser.add_argument("--check-artifacts", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception:
        # Tracebacks and transport errors can include a private origin.
        print("Native private download configuration failed; inspect sanitized receipts.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
