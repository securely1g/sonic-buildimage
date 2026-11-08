#!/usr/bin/env python3
"""Serve public GitHub source bytes through the native job's loopback listener.

This changes transport only. Bazel retains the original archive integrity, and
the original source URL remains a downloader candidate. API credentials never
enter downloader URLs, asset redirects, child processes, or retained receipts.
"""

import contextlib
import http.server
import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


PREFIX = "/_sonic_github_release/"
ARCHIVE_PREFIX = "/_sonic_github_archive/"
PATTERN = r"(github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^/?#]+/[^/?#]+)"
COMMIT_ARCHIVE_PATTERN = r"(github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/archive/([0-9a-f]{40})\.tar\.gz)"
ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
TOKEN_ENV = "SONIC_GITHUB_RELEASE_TOKEN"
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
TRANSFER_SECONDS = 900


class DownloadError(Exception):
    """A deliberately URL/credential-free transport error."""

    def __init__(self, message, *, category="validation"):
        super().__init__(message)
        self.category = category


FAILURE_CATEGORY = r"(?:metadata|asset):(?:transport|http:[345][0-9]{2}(?::rate_limited)?)|validation|other"


def http_failure(stage, error):
    category = f"{stage}:http:{error.code}"
    if error.headers and error.headers.get("X-RateLimit-Remaining") == "0":
        category += ":rate_limited"
    return category if re.fullmatch(FAILURE_CATEGORY, category) else "other"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_release(url):
    value = urllib.parse.urlsplit(url)
    parts = value.path.strip("/").split("/")
    if (value.scheme != "https" or value.netloc != "github.com" or value.query or value.fragment
            or len(parts) != 6 or parts[2:4] != ["releases", "download"]
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}
                   for name in parts[:2])):
        raise DownloadError("Unsupported public release URL")
    tag, name = (urllib.parse.unquote(part, errors="strict") for part in parts[4:])
    if (not tag or not name or "/" in name or name in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in tag + name)):
        raise DownloadError("Unsupported public release name")
    return "/".join(parts[:2]), tag, name


def archive_target(url):
    value = urllib.parse.urlsplit(url)
    match = re.fullmatch(COMMIT_ARCHIVE_PATTERN, value.netloc + value.path)
    if (value.scheme != "https" or value.query or value.fragment or not match
            or any(part in {".", ".."} for part in match[2].split("/"))):
        raise DownloadError("Unsupported public commit archive URL")
    return "https://codeload.github.com/" + match[2] + "/tar.gz/" + match[3]


class PublicAssets:
    def __init__(self, token="", opener=None):
        if (not isinstance(token, str) or len(token) > 4096
                or any(not 33 <= ord(character) <= 126 for character in token)):
            raise DownloadError("Invalid API credential")
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        self._assets = {}
        self._lock = threading.Lock()

    def _request(self, url, *, api=False, binary=False, archive=False):
        parsed = urllib.parse.urlsplit(url)
        allowed = {"codeload.github.com"} if archive else {"api.github.com"} if api else ASSET_HOSTS
        if (parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment
                or (archive and (api or parsed.query))
                or any(character.isspace() for character in url)):
            raise DownloadError("Unsupported release redirect")
        headers = {"User-Agent": "sonic-native-release-transport", "Accept-Encoding": "identity"}
        if api:
            headers.update({"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"})
        request = urllib.request.Request(url, headers=headers)
        if api and self._token:
            request.add_unredirected_header("Authorization", "Bearer " + self._token)
        return self._opener.open(request, timeout=60)

    def _json(self, path, *, repository_redirect=False):
        try:
            with self._request("https://api.github.com" + path, api=True) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if response.status != 200 or len(data) > 2 * 1024 * 1024:
                    raise DownloadError("Invalid public release metadata")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise DownloadError("Invalid public release metadata")
                return (value, None) if repository_redirect else value
        except urllib.error.HTTPError as error:
            category = http_failure("metadata", error)
            location = error.headers.get("Location", "") if error.headers else ""
            code = error.code
            error.close()
            if (repository_redirect and code in {301, 302, 307, 308}
                    and re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path)):
                target = urllib.parse.urlsplit(location)
                identity = re.fullmatch(r"/repositories/([1-9][0-9]{0,19})", target.path)
                if (target.scheme != "https" or target.netloc != "api.github.com"
                        or target.query or target.fragment or not identity):
                    raise DownloadError("Unsupported repository identity redirect") from None
                # Follow only one API-owned identity redirect. A second redirect
                # is refused by _json's normal no-redirect metadata policy.
                value = self._json(target.path)
                repository_id = int(identity[1])
                if type(value.get("id")) is not int or value["id"] != repository_id:
                    raise DownloadError("Repository identity changed") from None
                return value, repository_id
            raise DownloadError("Public release metadata unavailable", category=category) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise DownloadError("Public release metadata unavailable", category="metadata:transport") from None

    def resolve(self, url):
        repo, tag, name = parse_release(url)
        key = (repo, tag, name)
        with self._lock:
            if key not in self._assets:
                repository, redirected_id = self._json("/repos/" + repo, repository_redirect=True)
                canonical = repository.get("full_name", "")
                if (repository.get("private") is not False or repository.get("visibility") != "public"
                        or type(repository.get("id")) is not int or repository["id"] <= 0
                        or not isinstance(canonical, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", canonical)
                        or any(part in {".", ".."} for part in canonical.split("/"))
                        or (redirected_id is None and canonical.casefold() != repo.casefold())):
                    raise DownloadError("Only public repositories are allowed")
                release = self._json("/repos/" + canonical + "/releases/tags/" + urllib.parse.quote(tag, safe=""))
                if (release.get("draft") is not False or not release.get("published_at")
                        or release.get("tag_name") != tag):
                    raise DownloadError("Only published releases are allowed")
                candidates = release.get("assets", [])
                if not isinstance(candidates, list) or not all(isinstance(asset, dict) for asset in candidates):
                    raise DownloadError("Invalid release asset list")
                matches = [asset for asset in candidates if asset.get("name") == name]
                if len(matches) != 1:
                    raise DownloadError("Release asset is not unique")
                asset = matches[0]
                if (asset.get("state") != "uploaded" or type(asset.get("id")) is not int or asset["id"] <= 0
                        or type(asset.get("size")) is not int or not 0 < asset["size"] <= MAX_ASSET_BYTES):
                    raise DownloadError("Invalid release asset metadata")
                self._assets[key] = ("https://api.github.com/repos/" + canonical + "/releases/assets/" + str(asset["id"]), asset["size"])
            return self._assets[key]

    def open(self, url):
        target, size = self.resolve(url)
        for attempt in range(4):
            try:
                response = self._request(target, api=attempt == 0, binary=True)
                if (response.status != 200 or response.headers.get_content_type() in {"application/json", "text/html"}
                        or int(response.headers.get("Content-Length", size)) != size):
                    response.close()
                    raise DownloadError("Invalid release asset response")
                return response, size
            except urllib.error.HTTPError as error:
                location = error.headers.get("Location") if error.headers else None
                code = error.code
                category = http_failure("asset", error)
                error.close()
                if code != 302 or not location:
                    raise DownloadError("Public release asset unavailable", category=category) from None
                target = location
            except (OSError, ValueError, urllib.error.URLError):
                raise DownloadError("Public release asset unavailable", category="asset:transport") from None
        raise DownloadError("Too many release redirects")

    def open_archive(self, url):
        # This is the byte-identical destination of GitHub's commit archive
        # route, not the API's differently packaged legacy tarball. No API
        # credential, cookie or redirect is used for this public request.
        target = archive_target(url)
        try:
            response = self._request(target, archive=True)
            size = response.headers.get("Content-Length")
            try:
                size = int(size) if size is not None else None
                if (response.status != 200 or response.headers.get_content_type() in {"application/json", "text/html"}
                        or (size is not None and not 0 < size <= MAX_ASSET_BYTES)):
                    raise DownloadError("Invalid commit archive response")
            except Exception:
                response.close()
                raise
            return response, size
        except urllib.error.HTTPError as error:
            category = http_failure("asset", error)
            error.close()
            raise DownloadError("Public commit archive unavailable", category=category) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise DownloadError("Public commit archive unavailable", category="asset:transport") from None


def downloader_policy(original, port):
    # Bazel applies rewrites once. Cover already-declared release mirrors too
    # (for example the P4RT Fedora XZ URL's GitHub replacement), retaining each
    # original candidate in its original relative order.
    rows = []
    for line in original.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "rewrite" and fields[2].startswith("https://github.com/"):
            try:
                parse_release(fields[2])
            except (DownloadError, ValueError):
                pass
            else:
                preferred = f"rewrite {fields[1]} http://127.0.0.1:{port}{PREFIX}" + fields[2][8:]
                if not rows or rows[-1] != preferred:
                    rows.append(preferred)
        rows.append(line)
    policy = "\n".join(rows) + "\n"
    # Retain the original archive basename: Bazel infers its extraction type
    # from the first rewritten URL, while codeload's URL ends with a bare SHA.
    prefix = (f"rewrite {PATTERN} http://127.0.0.1:{port}{PREFIX}$1\n"
              f"rewrite {PATTERN} https://$1\n"
              f"rewrite {COMMIT_ARCHIVE_PATTERN} http://127.0.0.1:{port}{ARCHIVE_PREFIX}$1\n"
              f"rewrite {COMMIT_ARCHIVE_PATTERN} https://$1\n")
    return policy if policy.startswith(prefix) else prefix + policy


def loopback_environment(environment):
    """Bazel does not implicitly bypass an HTTP proxy for loopback URLs."""
    result = dict(environment)
    for key, other in (("no_proxy", "NO_PROXY"), ("NO_PROXY", "no_proxy")):
        original = environment.get(key, environment.get(other, ""))
        values = [part.strip() for part in original.split(",") if part.strip()]
        if "127.0.0.1" not in values:
            values.append("127.0.0.1")
        result[key] = ",".join(values)
    return result


@contextlib.contextmanager
def serve(token="", *, client=None, archive_counts=None):
    assets = client or PublicAssets(token)
    counts = {"completed": 0, "failed": 0, "failure_categories": {}}
    if archive_counts is None:
        archive_counts = {}
    archive_counts.update({"completed": 0, "failed": 0, "failure_categories": {}})
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            started = False
            selected_counts = counts
            try:
                prefix = next((value for value in (PREFIX, ARCHIVE_PREFIX) if self.path.startswith(value)), None)
                if not ipaddress.ip_address(self.client_address[0]).is_loopback or prefix is None:
                    self.send_error(403, "Only native loopback release requests are allowed")
                    return
                if prefix == ARCHIVE_PREFIX:
                    selected_counts = archive_counts
                url = "https://" + self.path[len(prefix):]
                response, size = assets.open_archive(url) if prefix == ARCHIVE_PREFIX else assets.open(url)
                with contextlib.closing(response):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    if size is not None:
                        self.send_header("Content-Length", str(size))
                    self.end_headers()
                    started = True
                    copied = 0
                    deadline = time.monotonic() + TRANSFER_SECONDS
                    while True:
                        if time.monotonic() > deadline:
                            raise DownloadError("Release asset transfer exceeded deadline")
                        limit = size if size is not None else MAX_ASSET_BYTES
                        data = response.read1(min(1024 * 1024, limit - copied + 1))
                        if not data:
                            break
                        copied += len(data)
                        if copied > limit:
                            raise DownloadError("Release asset size changed")
                        self.wfile.write(data)
                    if copied == 0 or (size is not None and copied != size):
                        raise DownloadError("Incomplete release asset")
                with lock:
                    selected_counts["completed"] += 1
            except Exception as error:
                # No exception text is safe here: upstream errors may contain
                # expiring asset signatures. Keep them off stderr and HTTP.
                with lock:
                    selected_counts["failed"] += 1
                    category = error.category if isinstance(error, DownloadError) else "other"
                    if not isinstance(category, str) or not re.fullmatch(FAILURE_CATEGORY, category):
                        category = "other"
                    selected_counts["failure_categories"][category] = selected_counts["failure_categories"].get(category, 0) + 1
                if not started:
                    try:
                        self.send_error(502, "Public release transport unavailable")
                    except OSError:
                        pass
                self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
