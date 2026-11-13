"""Describe only the already observed public source file, without changing APT."""

import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_apt_cache_policy as policy

SOURCE_SHA = "46b768cfd5db09b20052dccc2cfd365fa90277491b767035cfd8b0078b42d33c"
FIELDS = {"Types", "URIs", "Suites", "Components", "Signed-By"}
SUITES = {"trixie", "trixie-updates", "trixie-security"}
COMPONENTS = {"main", "contrib", "non-free", "non-free-firmware"}


def uri_shape(value):
    result = {"scheme": "other", "host": "other", "path": "other", "connection_fields_safe": False}
    try:
        uri = urlsplit(value)
        result["scheme"] = uri.scheme if uri.scheme in {"http", "https"} else "other"
        result["host"] = uri.hostname if uri.hostname in {"deb.debian.org", "security.debian.org"} else "other"
        result["path"] = uri.path.rstrip("/") if uri.path.rstrip("/") in {"/debian", "/debian-security"} else "other"
        result["connection_fields_safe"] = (uri.username is None and uri.password is None and
            uri.port in (None, 443 if uri.scheme == "https" else 80) and not uri.query and not uri.fragment)
    except ValueError:
        pass
    return result


def describe(data):
    """Return fixed known-value flags; never emit fields, URLs or source text."""
    policy.require(len(data) <= 16384)
    result = {"syntax": "ok", "paragraph_count": 0, "rows": []}
    paragraphs, current = [], {}
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        result["syntax"] = "invalid_encoding"
        return result
    for line in lines + [""]:
        if line.startswith("#"):
            continue
        if not line.strip():
            if current:
                paragraphs.append(current)
                current = {}
            if len(paragraphs) > 4:
                result["syntax"] = "too_many_paragraphs"
                return result
            continue
        key, colon, value = line.partition(":")
        if line[0].isspace() or not colon or key in current:
            result["syntax"] = "unsupported_line_or_duplicate_field"
            return result
        current[key] = value.strip()
    result["paragraph_count"] = len(paragraphs)
    for row in paragraphs:
        suites = set(row.get("Suites", "").split())
        components = set(row.get("Components", "").split())
        uris = row.get("URIs", "").split()
        result["rows"].append({
            "fields_exact": set(row) == FIELDS,
            "types_deb_only": row.get("Types") == "deb",
            "types_deb_and_source": set(row.get("Types", "").split()) == {"deb", "deb-src"},
            "keyring_exact": row.get("Signed-By") == str(policy.KEYRING),
            "suites_expected": bool(suites) and suites <= SUITES,
            "suites_with_backports_only": bool(suites) and suites <= SUITES | {"trixie-backports"},
            "components_expected": bool(components) and components <= COMPONENTS,
            "uri_count": len(uris),
            "uris": [uri_shape(value) for value in uris[:2]],
        })
    return result


def observe():
    policy.require(os.getuid() == 0)
    data, metadata = policy.read_public_file(policy.SOURCE, policy.SOURCE_DIR, 16384)
    policy.require(len(data) == 437 and hashlib.sha256(data).hexdigest() == SOURCE_SHA)
    names = []
    for index, path in enumerate(policy.SOURCE_DIR.iterdir()):
        policy.require(index < 64)
        if path.suffix in {".sources", ".list"}:
            names.append(path.name)
    result = {"schema": 1, "scope": "fixed_APT_source_structure_metadata_only",
              "source": metadata, "source_file_count": len(names),
              "source_inventory_exact": set(names) == {"debian.sources"},
              "shape": describe(data), "network": False, "APT_mutation": False,
              "fallback_activated": False}
    try:
        policy.official_sources(data)
        result["current_source_parser_accepts"] = True
    except (ValueError, UnicodeError):
        result["current_source_parser_accepts"] = False
    result["rejection_code"] = ("source_inventory" if not result["source_inventory_exact"] else
                                "source_shape" if not result["current_source_parser_accepts"] else "none")
    return result


if __name__ == "__main__":
    print(json.dumps(observe(), sort_keys=True))
