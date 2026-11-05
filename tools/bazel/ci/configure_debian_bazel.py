#!/usr/bin/env python3
"""Configure Bazel downloads in one ephemeral Debian runner user's home."""

import hashlib
import os
from pathlib import Path
import re
import shlex
from urllib.request import urlopen


DEBIAN_SNAPSHOT = 'snapshot.debian.org/archive/debian/20260727T143429Z/'
SECURITY_SNAPSHOT = 'snapshot.debian.org/archive/debian-security/20260726T121236Z/'
# Try verified official content paths for these exact snapshot inputs.
SNAPSHOT_FALLBACKS = {
    # Snapshot's binary API binds each file ID to the package/version/architecture;
    # Bazel still enforces the original package SHA256 after selecting this transport.
    SECURITY_SNAPSHOT + 'pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb':
        '66b33fe93d5d572e06cdabdd49d01c7b4947cfc7',
    SECURITY_SNAPSHOT + 'pool/updates/main/libs/libsodium/libsodium-dev_1.0.18-1+deb13u1_amd64.deb':
        '8637813e2c1b3ddb13dbb27c5fbd0bbf437610ed',
    SECURITY_SNAPSHOT + 'pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb':
        'a6e3b3b45aaf7799d6ab6ad71e09218a813866c8',
    SECURITY_SNAPSHOT + 'pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb':
        '6e3fe344496850bb946e702685bacc5df1332dab',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6-dev_2.41-12+deb13u3_arm64.deb':
        'b009545d0ef35bce018084a2ca524f3cbe3481e0',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6-dev_2.41-12+deb13u3_armhf.deb':
        'e8252ec26b774fc2153d6eb119bf65b83ebc2c04',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6_2.41-12+deb13u3_amd64.deb':
        '11449623c6859d9164dff8aedbcf023edaddc513',
    DEBIAN_SNAPSHOT + 'pool/main/p/python3.13/python3.13_3.13.5-2+deb13u4_amd64.deb':
        '8ebce1ad081382ee68e57abdc1834ba5041bc14c',
    # These Packages.xz bytes match SHA256 entries in signed metadata at the
    # same snapshot timestamp, verified by gpgv with Debian archive-keyring 2025.1.
    # Bookworm Release SHA256 abcf5882746e0f68171f41adbb4ac01b74b49d62d203379befb9265804311a4f:
    # index SHA256 9e0b5aabb2465b3d2e7a7fe27f9913846277833f7a2826e7767acccff5b588c5, 8790396 bytes.
    # Trixie InRelease SHA256 98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed:
    # index SHA256 3ab4e811cf4f3e5a335d382c58cc19d85f1abe7a4ef4689160ca1f637fa0e9b3, 9672648 bytes.
    # Official by-hash and /file bodies were byte-identical. SHA1 is only the
    # URL locator; rules_distroless' existing URL-scoped integrity facts are unchanged.
    DEBIAN_SNAPSHOT + 'dists/bookworm/main/binary-amd64/Packages.xz':
        'efe5b90ec48b9c017922c5aa4996807d0f044064',
    DEBIAN_SNAPSHOT + 'dists/trixie/main/binary-amd64/Packages.xz':
        'da72348e0606a7ab26beca8b5e07e1dd8a3a0a9f',
}

# These archive alternatives match the repository rules' unchanged SHA256 pins:
# rules_bison@0.4, bison 3.3.2: 039ee45b61d95e5003e7e8376f9080001b4066ff357bde271b7faace53b9d804
# rules_m4@0.3.1, m4 1.4.18: f2c1e86ca0a404ff281631bdc8377638992744b175afb806e25871a24a934e07
BISON_MIRROR = 'https://distfiles.macports.org/bison/bison-3.3.2.tar.xz'
M4_MIRROR = 'https://deb.debian.org/debian/pool/main/m/m4/m4_1.4.18.orig.tar.xz'
GNU_FALLBACKS = {
    'mirrors.kernel.org/gnu/bison/bison-3.3.2.tar.xz': BISON_MIRROR,
    'ftp.gnu.org/gnu/bison/bison-3.3.2.tar.xz': BISON_MIRROR,
    'ftpmirror.gnu.org/m4/m4-1.4.18.tar.xz': M4_MIRROR,
    'mirrors.kernel.org/gnu/m4/m4-1.4.18.tar.xz': M4_MIRROR,
    'ftp.gnu.org/gnu/m4/m4-1.4.18.tar.xz': M4_MIRROR,
}


def with_download_fallbacks(data):
    """Try exact verified transports before the unchanged registry config."""
    lines = ['# Exact fallbacks; original Bazel integrity behavior is unchanged.']
    fallbacks = {source: f'https://snapshot.debian.org/file/{identifier}'
                 for source, identifier in SNAPSHOT_FALLBACKS.items()}
    fallbacks.update(GNU_FALLBACKS)
    for source, destination in fallbacks.items():
        expression = re.escape(source)
        lines.extend([
            f'rewrite {expression} {destination}',
            f'rewrite {expression} https://{source}',
        ])
    return ('\n'.join(lines) + '\n\n').encode() + data


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
    config.write_bytes(with_download_fallbacks(data))
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
