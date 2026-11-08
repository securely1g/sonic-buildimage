#!/usr/bin/env python3
"""Public release identity, redirect credentials, and real loopback streaming."""

import contextlib
import email.message
import io
import json
from pathlib import Path
import re
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_release_transport as transport

URL = "https://github.com/example/tools/releases/download/v1.0/tool.tar.gz"
API = "https://api.github.com/repos/example/tools/releases/assets/17"
CDN = "https://release-assets.githubusercontent.com/asset?signature=private-test-signature"
BODY = b"byte-identical release archive\x00\xff"
TOKEN = "fixture-only-api-credential"


class Response(io.BytesIO):
    status = 200
    def __init__(self, data, content_type="application/json"):
        super().__init__(data)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(data))


class Origin:
    def __init__(self, *, private=False, draft=False, location=CDN, direct=False):
        self.calls = []
        self.repository = {"private": private, "visibility": "private" if private else "public",
                           "full_name": "example/tools"}
        self.release = {"tag_name": "v1.0", "draft": draft, "published_at": "2026-11-08T00:00:00Z",
                        "assets": [{"id": 17, "name": "tool.tar.gz", "state": "uploaded", "size": len(BODY)}]}
        self.location, self.direct = location, direct

    def open(self, request, timeout):
        self.calls.append(request)
        url = request.full_url
        if url == "https://api.github.com/repos/example/tools":
            return Response(json.dumps(self.repository).encode())
        if url == "https://api.github.com/repos/example/tools/releases/tags/v1.0":
            return Response(json.dumps(self.release).encode())
        if url == API:
            if self.direct:
                return Response(BODY, "application/octet-stream")
            raise urllib.error.HTTPError(url, 302, "Found", {"Location": self.location}, io.BytesIO())
        if url == CDN:
            return Response(BODY, "application/octet-stream")
        raise AssertionError("Unexpected origin request")


class ReleaseTransportTests(unittest.TestCase):
    def test_loopback_proxy_bypass_preserves_each_existing_case(self):
        for initial in ({}, {"no_proxy": ""}, {"NO_PROXY": "existing.example"},
                        {"no_proxy": "lower.example", "NO_PROXY": "upper.example"},
                        {"no_proxy": "127.0.0.1,other.example"}):
            with self.subTest(initial=initial):
                result = transport.loopback_environment(initial)
                for key, other in (("no_proxy", "NO_PROXY"), ("NO_PROXY", "no_proxy")):
                    expected = [value for value in initial.get(key, initial.get(other, "")).split(",") if value]
                    self.assertTrue(set(expected) <= set(result[key].split(",")))
                    self.assertEqual(result[key].split(",").count("127.0.0.1"), 1)

    def test_invalid_credentials_and_oversized_assets_fail_closed(self):
        for token in (None, "line\nbreak", "space value", "x" * 4097):
            with self.subTest(token=type(token).__name__), self.assertRaises(transport.DownloadError):
                transport.PublicAssets(token)
        origin = Origin()
        origin.release["assets"][0]["size"] = transport.MAX_ASSET_BYTES + 1
        with self.assertRaises(transport.DownloadError):
            transport.PublicAssets(TOKEN, origin).open(URL)
        self.assertEqual(len(origin.calls), 2)

    def test_exact_bytes_and_credential_free_redirect(self):
        origin = Origin()
        client = transport.PublicAssets(TOKEN, origin)
        with contextlib.closing(client.open(URL)[0]) as response:
            self.assertEqual(response.read(), BODY)
        self.assertEqual(len(origin.calls), 4)
        for request in origin.calls[:3]:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertIsNone(origin.calls[-1].get_header("Authorization"))
        self.assertNotIn(TOKEN, origin.calls[-1].full_url)
        self.assertEqual(origin.calls[2].get_header("Accept"), "application/octet-stream")

    def test_anonymous_and_direct_api_body(self):
        origin = Origin(direct=True)
        with contextlib.closing(transport.PublicAssets(opener=origin).open(URL)[0]) as response:
            self.assertEqual(response.read(), BODY)
        self.assertTrue(all(request.get_header("Authorization") is None for request in origin.calls))

    def test_private_repositories_and_drafts_cannot_use_api_credential(self):
        for kwargs, requests in (({"private": True}, 1), ({"draft": True}, 2)):
            with self.subTest(kwargs=kwargs):
                origin = Origin(**kwargs)
                with self.assertRaises(transport.DownloadError):
                    transport.PublicAssets(TOKEN, origin).open(URL)
                self.assertEqual(len(origin.calls), requests)

    def test_release_and_asset_identity_must_match(self):
        for field, value in (("tag_name", "different"), ("published_at", None), ("assets", [])):
            with self.subTest(field=field):
                origin = Origin()
                origin.release[field] = value
                with self.assertRaises(transport.DownloadError):
                    transport.PublicAssets(TOKEN, origin).open(URL)
                self.assertEqual(len(origin.calls), 2)

    def test_redirects_cannot_forward_credentials_or_escape_https_asset_hosts(self):
        for destination in ("http://release-assets.githubusercontent.com/file", "https://evil.invalid/file",
                            "https://api.github.com/repos/private/data", "https://user@objects.githubusercontent.com/file",
                            "https://objects.githubusercontent.com:444/file"):
            with self.subTest(destination=destination):
                origin = Origin(location=destination)
                with self.assertRaises(transport.DownloadError):
                    transport.PublicAssets(TOKEN, origin).open(URL)
                self.assertEqual(len(origin.calls), 3)

    def test_json_api_redirect_is_not_followed(self):
        class Redirect:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": CDN}, io.BytesIO())
        with self.assertRaisesRegex(transport.DownloadError, "metadata unavailable"):
            transport.PublicAssets(TOKEN, Redirect()).open(URL)
        self.assertIsNone(transport.NoRedirects().redirect_request(None, None, 302, None, {}, CDN))

    def test_url_parser_and_two_route_policy_are_narrow(self):
        self.assertEqual(transport.parse_release(URL), ("example/tools", "v1.0", "tool.tar.gz"))
        for url in (URL.replace("https:", "http:"), URL + "?token=secret", URL + "#fragment",
                    URL.replace("github.com", "github.com.evil.invalid"), URL.replace("tool.tar.gz", "%2Fetc"),
                    URL.replace("/example/", "/../")):
            with self.subTest(url=url), self.assertRaises(transport.DownloadError):
                transport.parse_release(url)
        original = "rewrite deb.example/(.*) https://private.example/$1\n"
        lines = transport.downloader_policy(original, 12345).splitlines()
        self.assertEqual(lines[-1] + "\n", original)
        match = re.fullmatch(lines[0].split()[1], URL.removeprefix("https://"))
        self.assertIsNotNone(match)
        self.assertEqual(lines[1].split()[2].replace("$1", match[1]), URL)
        self.assertNotIn(TOKEN, "\n".join(lines))
        self.assertIsNone(re.fullmatch(lines[0].split()[1], "github.com/example/tools/archive/v1.0.tar.gz"))

    def test_real_loopback_server_streams_exact_bytes_without_private_headers(self):
        origin = Origin()
        with transport.serve(client=transport.PublicAssets(TOKEN, origin)) as (port, counters):
            url = f"http://127.0.0.1:{port}" + transport.PREFIX + URL.removeprefix("https://")
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.read(), BODY)
                self.assertIsNone(response.headers.get("Location"))
                self.assertIsNone(response.headers.get("Authorization"))
        self.assertEqual(counters, {"completed": 1, "failed": 0})

    def test_transport_errors_never_disclose_signed_urls_or_credentials(self):
        class Broken:
            def open(self, url):
                raise RuntimeError(CDN + " " + TOKEN)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), transport.serve(client=Broken()) as (port, counters):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"http://127.0.0.1:{port}" + transport.PREFIX + URL.removeprefix("https://"))
            body = caught.exception.read().decode()
        self.assertEqual(counters, {"completed": 0, "failed": 1})
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("signature", body)


if __name__ == "__main__":
    unittest.main()
