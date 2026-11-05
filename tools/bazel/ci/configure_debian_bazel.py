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
KERNEL_SOURCE_BASE = 'packages.trafficmanager.net/public/debian-security/pool/updates/main/l/linux/'
# Try verified official content paths for these exact inputs.
SNAPSHOT_FALLBACKS = {
    # Snapshot's binary API binds each file ID to the package/version/architecture;
    # Bazel still enforces the original package SHA256 after selecting this transport.
    SECURITY_SNAPSHOT + 'pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb':
        '66b33fe93d5d572e06cdabdd49d01c7b4947cfc7',
    SECURITY_SNAPSHOT + 'pool/updates/main/libs/libsodium/libsodium-dev_1.0.18-1+deb13u1_amd64.deb':
        '8637813e2c1b3ddb13dbb27c5fbd0bbf437610ed',
    SECURITY_SNAPSHOT + 'pool/updates/main/g/gnutls28/libgnutls30t64_3.8.9-3+deb13u4_amd64.deb':
        '72f4c6cf757e4af0acf000271eec501c06f7a97d',
    SECURITY_SNAPSHOT + 'pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb':
        'a6e3b3b45aaf7799d6ab6ad71e09218a813866c8',
    SECURITY_SNAPSHOT + 'pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb':
        '6e3fe344496850bb946e702685bacc5df1332dab',
    DEBIAN_SNAPSHOT + 'pool/main/b/bubblewrap/bubblewrap_0.11.0-2+deb13u1_amd64.deb':
        '817a2e6186906394f63696c2b289f0f19e45b8d3',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6-dev_2.41-12+deb13u3_amd64.deb':
        'fbb9e46e11e1a8365a4f067eb9d6ec10251df100',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6-dev_2.41-12+deb13u3_arm64.deb':
        'b009545d0ef35bce018084a2ca524f3cbe3481e0',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6-dev_2.41-12+deb13u3_armhf.deb':
        'e8252ec26b774fc2153d6eb119bf65b83ebc2c04',
    DEBIAN_SNAPSHOT + 'pool/main/g/glibc/libc6_2.41-12+deb13u3_amd64.deb':
        '11449623c6859d9164dff8aedbcf023edaddc513',
    DEBIAN_SNAPSHOT + 'pool/main/p/python3.13/python3.13_3.13.5-2+deb13u4_amd64.deb':
        '8ebce1ad081382ee68e57abdc1834ba5041bc14c',
    # These bodies match package SHA256/size records retained by successful
    # archive run 41842800844; each /file copy equals its original URL body.
    SECURITY_SNAPSHOT + 'pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_amd64.deb':
        '3b5367f36e8989c6021e165214a9afe7a2110121',
    DEBIAN_SNAPSHOT + 'pool/main/p/pcre2/libpcre2-8-0_10.46-1~deb13u1_amd64.deb':
        '1594986c3db12e53c6d738df0926766b852e8e9d',
    DEBIAN_SNAPSHOT + 'pool/main/p/python3.13/libpython3.13-stdlib_3.13.5-2+deb13u4_amd64.deb':
        '34446b6d753038d30cc9dfb5bf17987ff56071da',
    DEBIAN_SNAPSHOT + 'pool/main/p/python3.13/python3.13-minimal_3.13.5-2+deb13u4_amd64.deb':
        '74e55d896b26f35fffd8863b6c23d5c47491f2a5',
    # The comerr original and /file body match its retained Bazel package pin:
    # SHA256 403200f03c6220e63e71d6a82111f7092fea0e9f62acf50822ee895c72cbfd8b, 56728 bytes.
    DEBIAN_SNAPSHOT + 'pool/main/e/e2fsprogs/comerr-dev_2.1-1.47.2-3+b11_amd64.deb':
        '9d33b1cb3fed84c29a8623cce03b2ba4d2cb8791',
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
    # Module resolution reads these indexes even for native AMD64 targets.
    # gpgv verified their InRelease files with Debian archive-keyring 2025.1:
    # main: 98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed
    # updates: 9604bf96c24a14c7212bb7ebc900d4b6d18cdd866144dfedf02ad5e395b890c0
    # security: c0891a1adb9e4d833fcaed4e0b0ca6107a67f22ff4832cf0e52327c7d127528c
    # Each original and named /file body matched the signed SHA256/size entry
    # listed below, and the two bodies were byte-identical.
    # main/arm64: 753da751bbc7a679f48bd1b623ffd4479cb6861c426118284c76eb82909e4908, 9607412 bytes.
    DEBIAN_SNAPSHOT + 'dists/trixie/main/binary-arm64/Packages.xz':
        '6aa348f0e9772d3c3506d135dde34bd41d6d1f86',
    # main/armhf: d459442ac471950615fa7e6267761bce16a63d756d29d14956ae315694a7810f, 9239704 bytes.
    DEBIAN_SNAPSHOT + 'dists/trixie/main/binary-armhf/Packages.xz':
        '65f7a0712e41b2ccd883ab98df318b31a4dc6d2d',
    # updates/amd64: 10012cb384dc2613586d7b3d391d92a5637c9faa36017239739f2a6109a0d45f, 4412 bytes.
    DEBIAN_SNAPSHOT + 'dists/trixie-updates/main/binary-amd64/Packages.xz':
        '5b6a8b61501276b8cb7e1892bea5b219018f4f00',
    # updates/arm64: fe771d58d4af39dbb94142d6f3ce578d340c6b1364c172d9e87f2287bdc4994b, 4424 bytes.
    DEBIAN_SNAPSHOT + 'dists/trixie-updates/main/binary-arm64/Packages.xz':
        'c9d85a145fcc34817defe65c818ded141ea2c95f',
    # updates/armhf: c9eb15a380077ab6af13dbc6fb76958e8b96b939cdcb0e999c8e582e619b0fd6, 4408 bytes.
    DEBIAN_SNAPSHOT + 'dists/trixie-updates/main/binary-armhf/Packages.xz':
        'ba24766d03291a7cbebe54ae2238ccec3c7861ea',
    # security/amd64: 278bbe9c144f40ddba2af39c9d151349de237542b90e8bec617f63eccba74cba, 226940 bytes.
    SECURITY_SNAPSHOT + 'dists/trixie-security/main/binary-amd64/Packages.xz':
        '8c7898c09763411a9bff9e68bab2c6f08a6b14b4',
    # security/arm64: 15a9424747de11c0bd6c414d38a32f995544baebda824558e2b2dab80b740c2a, 227976 bytes.
    SECURITY_SNAPSHOT + 'dists/trixie-security/main/binary-arm64/Packages.xz':
        '4e0a5b5589a7554f7fdf92fcf5d9d23a45ce6d4c',
    # security/armhf: 061362fef710f1810328bddba3b9c49d582838512a7376803708b1e4adcb54b9, 208284 bytes.
    SECURITY_SNAPSHOT + 'dists/trixie-security/main/binary-armhf/Packages.xz':
        'fb16158890603c0c5d37211213b40679f9b008d1',
    # These bodies match the unchanged kernel sources.bzl SHA256 pins:
    # dsc: 65bbf4e35635465326c9470605da1886ce3d3cf7e2a6d93392c4c6245d895b34
    # orig: 78afa637ca891174c22b332e4c1f87cf6aaa81861a6cca3b529e861843fa2fd3
    # debian: b62680107fc155ad3d2c421ba0af1d5848812674fe767fcd40ba81a414e4ce2a
    KERNEL_SOURCE_BASE + 'linux_6.12.41-1.dsc':
        '4c27cc03165101daa02730a246b5db5edfd2578e',
    KERNEL_SOURCE_BASE + 'linux_6.12.41.orig.tar.xz':
        'accbbe886f7ee7058c827f6ea981e6c57fca8285',
    KERNEL_SOURCE_BASE + 'linux_6.12.41-1.debian.tar.xz':
        '9580fbacfe8e5e345f4770237739808dfcc6af45',
}

# These exact official pool bodies match the same retained Bazel package hashes.
DEBIAN_POOL_FALLBACKS = {
    SECURITY_SNAPSHOT + 'pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb':
        'https://deb.debian.org/debian-security/pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb',
    SECURITY_SNAPSHOT + 'pool/updates/main/k/krb5/libkadm5clnt-mit12_1.21.3-5+deb13u1_amd64.deb':
        'https://deb.debian.org/debian-security/pool/updates/main/k/krb5/libkadm5clnt-mit12_1.21.3-5+deb13u1_amd64.deb',
    SECURITY_SNAPSHOT + 'pool/updates/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb':
        'https://deb.debian.org/debian/pool/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb',
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


def preserve_snapshot_filenames(data):
    """Keep archive suffixes in the verified config's bare Snapshot rewrites."""
    return re.sub(
        rb'(?m)^(rewrite [^\s()]+/)([^\s/()]+)( https://snapshot\.debian\.org/file/[0-9a-f]{40})$',
        rb'\1(\2)\3/$1', data)


def with_download_fallbacks(data):
    """Add exact fallbacks and retain names in the verified registry config."""
    lines = ['# Exact fallbacks; original Bazel integrity behavior is unchanged.']
    fallbacks = {source: f'https://snapshot.debian.org/file/{identifier}/{source.rsplit("/", 1)[-1]}'
                 for source, identifier in SNAPSHOT_FALLBACKS.items()}
    fallbacks.update(DEBIAN_POOL_FALLBACKS)
    fallbacks.update(GNU_FALLBACKS)
    for source, destination in fallbacks.items():
        expression = re.escape(source)
        lines.extend([
            f'rewrite {expression} {destination}',
            f'rewrite {expression} https://{source}',
        ])
    return ('\n'.join(lines) + '\n\n').encode() + preserve_snapshot_filenames(data)


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
