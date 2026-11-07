#!/usr/bin/env python3
"""Give one native CI APT invocation a disposable public Debian source view.

The worker's HTTP adapter retrieves these files over verified upstream HTTPS.
Keep the image's source files and all APT signature/checksum settings unchanged.
"""

import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


DEBIAN_ORIGINS = re.compile(
    r"https://(deb\.debian\.org|security\.debian\.org|ftp\.debian\.org|"
    r"debian-archive\.trafficmanager\.net|snapshot\.debian\.org|"
    r"snapshot-cloudflare\.debian\.org)(?=/)")
def source_routes(uri, suite, components, stamps):
    parsed = urlsplit(uri)
    if (parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password or parsed.port):
        return [(uri, components)]
    path = parsed.path.strip("/").split("/")
    if parsed.hostname in {"deb.debian.org", "security.debian.org", "ftp.debian.org",
                           "debian-archive.trafficmanager.net"} and len(path) == 1 and path[0] in stamps:
        archive, stamp = path[0], stamps[path[0]]
    elif (parsed.hostname in {"snapshot.debian.org", "snapshot-cloudflare.debian.org"}
          and len(path) == 3 and path[0] == "archive" and path[1] in stamps
          and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", path[2])):
        archive, stamp = path[1], path[2]
    else:
        return [(uri, components)]
    snapshot = "https://snapshot.debian.org/archive/" + archive + "/" + stamp + "/"
    # The pinned private mirror serves main, without backports. Retain the
    # other components and their pool origin on that same signed snapshot.
    if suite == "trixie-backports":
        return [(snapshot, components)]
    routes = []
    if "main" in components:
        routes.append((DEBIAN_ORIGINS.sub(r"http://\1", uri), ["main"]))
    other = [component for component in components if component != "main"]
    if other:
        routes.append((snapshot, other))
    return routes or [(uri, components)]


def source_view(content, suffix, stamps):
    if suffix != ".sources":
        pattern = re.compile(r"(?m)^([ \t]*deb(?:-src)?[ \t]+(?:\[[^\]\n]*\][ \t]+)?)"
                             r"(\S+)[ \t]+(\S+)[ \t]+([^#\n]+)([^\n]*)$")
        def line(match):
            return "\n".join(match[1] + uri + " " + match[3] + " " + " ".join(components)
                             + (" " + match[5] if match[5] else "")
                             for uri, components in source_routes(match[2], match[3], match[4].split(), stamps))
        return pattern.sub(line, content)
    blocks = []
    for block in re.split(r"\n[ \t]*\n", content):
        fields = {name: re.search(r"(?mi)^" + name + r":[^\n]*(?:\n[ \t][^\n]*)*", block)
                  for name in ("Suites", "URIs", "Components")}
        if not all(fields.values()):
            blocks.append(block)
            continue
        values = {name: match[0].partition(":")[2].split() for name, match in fields.items()}
        for uri in values["URIs"]:
            for suite in values["Suites"]:
                for routed, components in source_routes(uri, suite, values["Components"], stamps):
                    selected = block
                    for name, value in (("URIs", routed), ("Suites", suite),
                                        ("Components", " ".join(components))):
                        selected = selected.replace(fields[name][0], name + ": " + value, 1)
                    blocks.append(selected)
    return "\n\n".join(blocks)


def apt_settings():
    output = subprocess.check_output([
        "apt-config", "shell", "source", "Dir::Etc::sourcelist/f",
        "parts", "Dir::Etc::sourceparts/d", "https_proxy", "Acquire::https::Proxy",
    ], text=True)
    result = dict(binding.split("=", 1) for binding in shlex.split(output))
    if not result.get("source") or not result.get("parts"):
        raise ValueError("APT source locations are required")
    return result


def copy_source(source, destination, stamps):
    # Keep every suite/component and authentication setting, including embedded
    # deb822 keys. Only the temporary view selects the download transport.
    content = source.read_text(encoding="utf-8")
    destination.write_text(source_view(content, source.suffix, stamps), encoding="utf-8")
    destination.chmod(stat.S_IMODE(source.stat().st_mode) & 0o644)


def run(command, arguments):
    stamps = {"debian": os.environ.get("SONIC_NATIVE_DEBIAN_SNAPSHOT", ""),
              "debian-security": os.environ.get("SONIC_NATIVE_DEBIAN_SECURITY_SNAPSHOT", "")}
    if not all(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp) for stamp in stamps.values()):
        raise ValueError("Native APT requires the pinned Debian snapshot timestamp")
    # A separate caller config or source-path override would invalidate the
    # source view selected by apt-config. Fail closed instead of ignoring it.
    for argument in arguments[:arguments.index("--") if "--" in arguments else len(arguments)]:
        if (argument.startswith(("-c", "--config")) or "dir::etc" in argument.lower()):
            raise ValueError("Native APT does not accept caller source/config overrides")
    settings = apt_settings()
    environment = dict(os.environ)
    https_proxy = environment.get("https_proxy", environment.get("HTTPS_PROXY", ""))
    environment["https_proxy"] = https_proxy
    environment["HTTPS_PROXY"] = https_proxy
    with tempfile.TemporaryDirectory(prefix="sonic-native-apt-") as temporary:
        root = Path(temporary)
        # APT's calling process parses the source files; download methods get
        # the selected URIs. Keep this view private, including file names.
        source = root / "sources.list"
        source.write_text("")
        source.chmod(0o644)
        original = Path(settings["source"])
        if original.exists():
            copy_source(original, source, stamps)
        parts = root / "sources.list.d"
        parts.mkdir(mode=0o755)
        originals = Path(settings["parts"])
        for original in sorted(originals.glob("*")):
            if original.suffix in {".list", ".sources"} and original.is_file():
                copy_source(original, parts / original.name, stamps)
        options = ["-o", "Dir::Etc::sourcelist=" + str(source),
                   "-o", "Dir::Etc::sourceparts=" + str(parts)]
        # BuildKit can omit an explicitly empty HTTPS proxy build argument.
        # Prevent APT inheriting the HTTP-only adapter for unrelated HTTPS URLs.
        if not https_proxy and not settings.get("https_proxy"):
            options += ["-o", "Acquire::https::Proxy=DIRECT"]
        return subprocess.run([command, *options, *arguments], env=environment).returncode


if __name__ == "__main__":
    sys.exit(run(sys.argv[1], sys.argv[2:]))
