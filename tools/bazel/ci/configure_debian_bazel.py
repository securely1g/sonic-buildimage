#!/usr/bin/env python3
"""Configure Bazel downloads in one ephemeral Debian runner user's home."""

import hashlib
import os
from pathlib import Path
import shlex
from urllib.request import urlopen


def configure():
    trust = Path('/etc/ssl/certs/java/cacerts')
    if not trust.is_file() or trust.stat().st_size == 0:
        raise SystemExit('Install ca-certificates-java before configuring Bazel')
    # Exact URL rewrites retain Bazel's original source and patch hashes.
    url = ('https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/'
           '3bd138ad4f0485a916ce9c729198f4299cd3bb3b/ci/source_mirrors.cfg')
    expected = '7c8f6684838354f3f6a9ee09a1bbc746a9c87a09a760c1e7aa9558019117946d'
    expected_size = 8919
    with urlopen(url, timeout=60) as response:
        data = response.read(expected_size + 1)
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit('Source mirror configuration checksum mismatch')
    # Separate root and non-root callers, even when they share RUNNER_TEMP.
    config = Path(os.environ['RUNNER_TEMP']) / f'sonic-source-mirrors-{os.getuid()}.cfg'
    config.write_bytes(data)
    settings = (
        'startup --host_jvm_args=-Djavax.net.ssl.trustStore=' + str(trust),
        'common --experimental_downloader_config=' + shlex.quote(str(config)),
    )
    rc = Path.home() / '.bazelrc'
    contents = rc.read_text() if rc.exists() else ''
    missing = [setting for setting in settings if setting not in contents.splitlines()]
    if missing:
        with rc.open('a') as output:
            output.write(('' if not contents or contents.endswith('\n') else '\n')
                         + '\n'.join(missing) + '\n')


if __name__ == '__main__':
    configure()
