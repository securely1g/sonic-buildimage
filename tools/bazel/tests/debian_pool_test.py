#!/usr/bin/env python3
"""Check locked package transport and its inherited CI override reach."""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock
import re
import shlex
import unittest
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG = "tools/bazel/debian-pool.cfg"

RUST_ARCHIVE_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# The three published gawk locations retain their original SHA256 and fallback.
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://mirrors.kernel.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftpmirror.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz

# Exact BCR ARM archive URLs only; preserve their SHA512 integrity and versions.
# Multiple matching rewrites are tried in order: verified GitHub copy, then upstream.
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/ape-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/ape/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/download_utils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/download_utils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_coreutils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_coreutils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_gzip-v1.0.0.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_gzip/-/releases/v1.0.0/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/toolchain_utils-v1.0.2.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/toolchain_utils/-/releases/v1.0.2/downloads/src.tar.gz

# This exact retired libcap-dev package has an available content-addressed URL
# even when Debian's dated archive path returns 503. SHA1 is only its URL locator;
# the original APT-lock SHA256 remains authoritative:
# cabb54ec097f89dd130902689a6bf38a67782fbf2aeca5f226ea656468843ec7
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap-dev_2\.75-10\+deb13u1\+b1_amd64\.deb https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6

# Preserve the retired libaudit-dev package through Debian's content address.
# Original APT-lock SHA256:
# 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524

# Exact ARM64 packages mirrored byte-for-byte after snapshot download failures.
# Original APT-lock SHA256 for libcap2:
# c9a77aa670c9ca163c624a00e55c4419df40b770fe516010dc495c04a16c80b0
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75-10\+deb13u1\+b1_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_arm64.deb
# Original APT-lock SHA256 for Bash:
# f9b0e4eb4ae81b3934a3211d755de26fa03881af3f159eb582437d3e8f55d9eb
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/b/bash/bash_5\.2\.37-2\+b9_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/bash_5.2.37-2%2Bb9_arm64.deb

# Exact retained Debian packages mirrored byte-for-byte after upstream outages.
# Original APT-lock SHA256: ec6c7a6770d1ff5295cf27b61436ec61b31d14bace3bff4fff336e3008818124; 408140 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap\-dev_2\.75\-10\+deb13u1\+b1_armhf\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap-dev_2.75-10%2Bdeb13u1%2Bb1_armhf.deb
# Original APT-lock SHA256: edeacbdd6a86ee82d314dccc180b740905fd7f6392b72e5c0533079fda449361; 914268 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/s/sqlite3/libsqlite3\-0_3\.46\.1\-7\+deb13u1_amd64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libsqlite3-0_3.46.1-7%2Bdeb13u1_amd64.deb

# Exact retained PCRE runtime imported during PR196 AMD64 Orchagent analysis.
# SHA256: fde8f26dfcc7073516c745381cf9940c964ce22e7458826dae5946b196c6115f; 259492 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-8\-0_10\.46\-1\~deb13u1_armhf\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libpcre2-8-0_10.46-1.deb13u1_armhf.deb


# Exact libcap2 AMD64 runtime selected by the current PR163 image lock.
# Original APT-lock SHA256: 860db6f80d400239744eee2b454f1d8e66b064e18dd0527535824887691e9cb7; 28988 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75\-10\+deb13u1\+b1_amd64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_amd64.deb

# Exact packages selected by native ARM CI, using verified Debian content URLs.
# The SHA1 locates the file; each original APT-lock SHA256 below is unchanged.
# libc-bin SHA256: 02f366115bea79b87fe26c7dec4dc444f9fdcc76440905d88cc26dbef075511b
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc-bin_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/2f97c88e63e698b86775350598dea8efb421df45
# uuid-dev SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

# Exact FRR AMD64 validation inputs: libperl is a native dependency; uuid-dev
# ARM64 is imported during AMD64 sysroot analysis, without ARM execution.
# SHA1 locates the bytes; each original APT-lock SHA256 remains authoritative.
# libperl5.40 SHA256: f20aa427c3f56f430b7b58b73222edcb8cda40fa9c3b25365b20cd53eddd51a9
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/libperl5\.40_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/115f4cbe17c3706c068bab13e8659621c15591f2
# uuid-dev ARM64 SHA256: 142f2c809a9c239b94a1a09dccc02a49cc4b20990da687b60f849cda84dd580a
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_arm64\.deb https://snapshot.debian.org/file/cb5244c39df10c7ce2573dbf21944fe03d8a436b

# Eventd AMD64 tests use this exact private Redis package. Keep its locked bytes.
# SHA256: 0edc72458f66c68f2d4ccd0c9959a85a81f4ec7de410a28861cde4a2059f245c
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/file/c502f81818978156937bce72be28acac56cf8da6
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb

# These three security-package exceptions retain their exact original URLs.
# libc-ares-dev also has the same bytes available in the official security CDN.
# libc-ares-dev SHA256: 40d2336fb01e4b707ad8a6bfbad522fc7ddff3b057d16ee981237fb7cef5d95c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/109d7800bf370139268bea60e2ea7e2699cc3d48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
# openssl-provider-legacy SHA256: b099729f6a38d7d57f7f442f0f836c862673b9bb0ee65d757da6e3cdebbfd2a4
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/d5a41e70f8551fb076606ed0b1c0dab7fa9bc32d
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_arm64.deb
# openssl SHA256: 1ee735e2480519bd1fd5f6576a879243bc81b9773059e1fa1ec8b8943c84e77c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/4b411e66ee3d3106570389d5d1e022b15c1975a8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3.5.6-1~deb13u2_arm64.deb

# Retained exact historical inputs for declared tools and SONiC source images.
# Content IDs locate bytes; the original locked SHA256 remains authoritative.
# SHA256: ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 0def2d972310b59704ad119abee5a97f95409e14ff1359edd8cc7b8892cfd43f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-stdlib_3\.13\.5-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/34446b6d753038d30cc9dfb5bf17987ff56071da
# SHA256: 11ec93d44649e9b37e3bddcf32a5f01d6e9dd3d0d79aac621b49473539c8f5b8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/perl_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/9c5005df956b8fa8a3f487c175499e6029669465
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354
# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: fed7de7d14f7df765bb6ef1a79bd55b90475808793a5c41dc9ccb9deacf639ae
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif-plugin-dav1d_1\.19\.8-1_amd64\.deb https://snapshot.debian.org/file/4db82883543536a32eb80350a3ac4149d85009de
# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100
# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88
# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\.7\.1-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9
# SHA256: c98b1130f08ef6ac3932972c56be84cfc308ce1afa37105cb06aaf47ced2d078
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\.13-1_amd64\.deb https://snapshot.debian.org/file/17e2275e726dad554ab38aa3f3d240f5270923cb
# SHA256: 8ffd13165b9ee3f067e2ee670df718e48c1bdaa18676ac93d1de761dbbb3913c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/11449623c6859d9164dff8aedbcf023edaddc513
# SHA256: ff529924782d3286181188fc265a6a92e7fe28975fb3a925dc0e05c0ca66e52f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/8dd0cfbe0f5bd2c3923c8252c00c868bc6ac945e
# Exact packages whose dated URLs failed during the ARM cross-build survey.
# SHA256: 10247744737c1bb574f9667b3f06a3b43a5055c67bdfcc1945803451cc036db3
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libuuid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/69561687e45ee4833a59400e51976ff05cf71c0d
# SHA256: 1652525c1c605b2169b3b27beecd01cbaa0e5abd7ec4d2029cbae2084dbde012
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/x/xapian-core/libxapian30_1\.4\.29-3_arm64\.deb https://snapshot.debian.org/file/b3cef6d6dfd6511f04a77442026f81811313012e
# SHA256: fc167d04049dfcf91ccff8687222a186be681cac2298ae89cf8523518ad04c88
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/file/a6e3b3b45aaf7799d6ab6ad71e09218a813866c8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb
# SHA256: ff88e52a6b6599851f3d44f7223f15123d9e5a0b02ed9a308c05c31fa88f7d73
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/3e0610e3b6f6d57539344d7a1f95da1ed442fd48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb
# SHA256: bed93f11942524e5338074c46cce376197740635bf0745ccb7d259e0f6ce7117
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libblkid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/8413cb58799327072dd69a418e41fc05009cfd03
# SHA256: fd62da71db387aba934e4bd17cceaf0db1454ae9e1eae378c4d8329fbe856883
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-dev_3\.13\.5-2\+deb13u4_arm64\.deb https://snapshot.debian.org/file/f0907e7116933f0437ac671e9946e4740fcef10b
# SHA256: 6bb2038f36817ae1ee6d33df03917b21835dcc2e0f3e3e80a71390e83c171173
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/file/faa5bddd33fff3675e7b76863c6ef140347fb3b2
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_amd64.deb
# SHA256: 879e8477dc20006e59788831e4f3a436d15afe3c055ae0e93beb96696f0e5eb1
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/libss2_1\.47\.2-3\+b11_amd64\.deb https://snapshot.debian.org/file/458f436ed67e7f80aed736c6665c6b725301024c
# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb

# Exact Debian patch archive used by source-base-image socat@1.8.0.2.sonic.3.
# SHA256 04b361cd91884eda6ca9fe2890ca83a6abe73dd8ce75fe9927b2d8443af36123; the SHA1 URL is only a locator.
# All other source archives stay outside the generic binary-package rewrites.
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/file/0475522901bd85478b944520b23e01e3f3c7d339
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1.8.0.2-1.debian.tar.xz

# Exact sibling architectures from the same failed consumers' unchanged APT locks.
# SHA256: 04250d9e51fc87ef6d539f36cf0b66668dd34beec101af56dc35198ea99cef00
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/ed4549f189f5faa3929d51268c16419c2ee87616
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_amd64.deb
# SHA256: afdd9c57261502a3a434ba606ddfd1e8fd0f288c663f2dc4f7570ce927867651
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/e00e42b454c6acfd3a11622bc8aca25e8b4a232b
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_arm64.deb
# SHA256: b2dab3fe29c1fcb82ef58c2db0ff4bd830a442cf37b9a8b730b7b6ce87181349
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/fa81f709727b16de2a82710bbe0556b58f2b3f50
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_amd64.deb
# SHA256: f7e09a12ccb2d6bd28bcc87a6e2462a0558dcb3cc34d56f90ee86fbd3402d8ba
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/6e3fe344496850bb946e702685bacc5df1332dab
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb
# SHA256: b574fdbc1ac7a50df51f382a23847a68687892ceb06dcf0b1bc1ed4730109c28
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/file/70634fafccf8dee1bbccd0091a321e81f8983755
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_armhf.deb
# SHA256: f875f56675be5b074da877f9a93b09d47dc2eb4e679951d36e2943b8d4843344
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/2df60d569f5ba6703427c701e3707c04cd958a03
# SHA256: 7f6868227f4893a11123e43b9ed291950b550c7dc209905b2fb871c74678d1bf
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_arm64\.deb https://snapshot.debian.org/file/48585a6c3fab8508da2a2702d18a16f3c605b9ab
# SHA256: 1c82e8bb5d628ca8f8343975b26578d0894b8a1c33b54bc7741a6bfa8d880654
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_armhf\.deb https://snapshot.debian.org/file/565675e8541721cdfa011219b0eba6efebf6f2d7
# SHA256: dabada7ca12f95a17a31d19c3be4a8708a6a37207ac3b46cd1c344503825b0ee
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-dev_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/3cb20569f34ff9c1f8537215183f7ce639a343f7
# SHA256: 85b7d04605f9d423d5699b3c07f027e8380649fd164a51e0712cded0cc56eced
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_arm64\.deb https://snapshot.debian.org/file/0cc7ca3341253904256e5dc4296786ce151e51eb
# SHA256: 1a4b6e94cbc26e03e791f7a6e5ae714adc03ee5fe5b24c01651d1194d9de3a3c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_armhf\.deb https://snapshot.debian.org/file/5563fcda49878ea86b02546db7c47c42af9a0a72

# Observed PR195 source-base/native analysis inputs; exact immutable package identities only.
# SHA256: 62c3dbd746163878e73e22206d1dcc01f0f5296d3ca4ece5fb8cb7cb80396e4f; 16204 bytes.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/4fef5fafa9bcf0df24e4cc6ad971283da129ffa7
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5-dev_1.21.3-5+deb13u1_amd64.deb
# SHA256: 6aa452af6a07e34498bf8e734e8789c9dd602fb34f12572e62dc1bba4889a60e; 298536 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-8\-0_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/1594986c3db12e53c6d738df0926766b852e8e9d
# SHA256: b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef; 852668 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd
# SHA256: cf70657d734060bad45def0c67208e2afb1e165a5640d53b3bfe2264a3ada932; 750500 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_arm64\.deb https://snapshot.debian.org/file/f114a29adbd36f6c0b136ab62613226f066e1fbf
# SHA256: a9210d107c63761a88847c0fdfcb7b15bcf7cd1fc443b91a25e58805a6f4f83d; 756112 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_armhf\.deb https://snapshot.debian.org/file/e4ad4f9737d3906da5f6cdbb819176ee0ac0ad57

# Exact FreeType runtime dependencies from the unchanged sonic-build-infra .28 APT lock.
# SHA256: e4947f3291528f03d574f2b01d5c5fc45c58c47480f32888a5cdf0f231ab584e
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/file/2602e72ebeb8d5aa36eb1cc60f1cabfbd7558f43
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
# SHA256: 0c426e83f1af816b9a4df9056fd2238463d1cd07969ee640965da9fb6ecaf107
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/218fd3daa2391ba367cdd38ff4f8f79f6b3757e1
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb

# Exact rsyslog-relp input from the PR163 source-base AMD64 analysis failure.
# SHA256: 8b58d38a801e33182b5bf66382f012830e8cd8b234358f76657eb946741a87ae; 316252 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/r/rsyslog/rsyslog\-relp_8\.2504\.0\-1_amd64\.deb https://snapshot.debian.org/file/c16b59f13516c30140c256c1127b3be80899aff7

# Exact JQ and ss-dev inputs observed by AMD64 SWSS/FRR image analysis.
# SHA256: 140ac37e4b35370c3f34c95407cba75b35570e0be4bb9eebbf40bd72d182257c; 164336 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/libjq1_1\.7\.1\-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/b8b613de6336c793442ca724edcb30be97ee3de0
# SHA256: 6792fc1b77c4335e780cf93fe5830a7930b392af2be0577cf25cb583bb4c1d48; 34996 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/ss\-dev_2\.0\-1\.47\.2\-3\+b11_armhf\.deb https://snapshot.debian.org/file/e2267362c7557b71bcdc844929316ebfe8256dc3

# Exact libexpat development input from SWSS AMD64 Python-target analysis.
# SHA256: ac1e7f8ad81578b61bc7c54c725ec639b06684e560766eac278490a1b2064372; 160568 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1\-dev_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/d718845c00344d7965787ca08877a6d1bc51ef71

# Exact warning inputs observed alongside the fatal SWSS libexpat download.
# SHA256: 3a0acd8b37955c0e102c756b52c97df2a31f67b96453c35dab70df218d309117; 33124 bytes.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/fdcbf424c12036172b67ed2d13d14fa441fb3736
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1.21.3-5+deb13u1_amd64.deb
# SHA256: 490511f32afe59303e1fa2b4c54fd174de0d98f5f3cf9702e0f2784b834792e4; 56944 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/comerr\-dev_2\.1\-1\.47\.2\-3\+b11_arm64\.deb https://snapshot.debian.org/file/9ba5541a96f7de01c0d343e78296b07e2672051f

# Exact ARM64 runtimes selected by native PI and P4C dependency analysis.
# SHA256: 2e791f8faf7bbbc94d354cf7b6c3728dc303b7aa327dfe7a2aa6954320977c8f; 198880 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_arm64\.deb https://snapshot.debian.org/file/81aee071e0a7fff9504c0ec2cbf6acf665ebf0bb
# SHA256: 208e9fdc14ed88a30f31fdfb993d1e8ab19f9ab29f3cd8546ffb4ea3f74f5a3f; 325232 bytes.
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://snapshot.debian.org/file/5e4fec5c53e730725b92a01b3e6663878562fa79
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_arm64.deb

# Timestamped Debian binary packages: same pool path on the official CDN first,
# then the exact snapshot URL. Bazel still checks the locked package SHA256.
# These generic rules exclude indexes, source archives, and debian-security.
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2

# Cloudflare snapshot package inputs retain CDN, standard snapshot, then original.
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot-cloudflare.debian.org/archive/debian/$1/$2
"""

SHARED_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# Exact Bookworm security header payload; the repository SHA256 remains
# ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f.
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248

# Prefer the official pool for pinned Debian binary packages. Bazel keeps the
# repository rule's checksum; the original snapshot remains a fallback.
# Indexes and source archives must retain their snapshot URLs.
rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian/pool/$2

# Exact historical AMD64 payloads selected by declared build tools and source
# images. Reuse their verified content IDs before the dated Snapshot fallback.
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354

# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2\-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524
# SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41\-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian-security/pool/$2
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
"""


# Exact FRR/Eventd/OCI archive transport input; other jobs retain their existing pins.
SOURCE_IMAGE_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# The three published gawk locations retain their original SHA256 and fallback.
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://mirrors.kernel.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftpmirror.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz

# Exact BCR ARM archive URLs only; preserve their SHA512 integrity and versions.
# Multiple matching rewrites are tried in order: verified GitHub copy, then upstream.
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/ape-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/ape/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/download_utils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/download_utils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_coreutils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_coreutils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_gzip-v1.0.0.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_gzip/-/releases/v1.0.0/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/toolchain_utils-v1.0.2.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/toolchain_utils/-/releases/v1.0.2/downloads/src.tar.gz

# This exact retired libcap-dev package has an available content-addressed URL
# even when Debian's dated archive path returns 503. SHA1 is only its URL locator;
# the original APT-lock SHA256 remains authoritative:
# cabb54ec097f89dd130902689a6bf38a67782fbf2aeca5f226ea656468843ec7
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap-dev_2\.75-10\+deb13u1\+b1_amd64\.deb https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6

# Preserve the retired libaudit-dev package through Debian's content address.
# Original APT-lock SHA256:
# 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524

# Exact ARM64 packages mirrored byte-for-byte after snapshot download failures.
# Original APT-lock SHA256 for libcap2:
# c9a77aa670c9ca163c624a00e55c4419df40b770fe516010dc495c04a16c80b0
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75-10\+deb13u1\+b1_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_arm64.deb
# Original APT-lock SHA256 for Bash:
# f9b0e4eb4ae81b3934a3211d755de26fa03881af3f159eb582437d3e8f55d9eb
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/b/bash/bash_5\.2\.37-2\+b9_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/bash_5.2.37-2%2Bb9_arm64.deb

# Exact packages selected by native ARM CI, using verified Debian content URLs.
# The SHA1 locates the file; each original APT-lock SHA256 below is unchanged.
# libc-bin SHA256: 02f366115bea79b87fe26c7dec4dc444f9fdcc76440905d88cc26dbef075511b
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc-bin_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/2f97c88e63e698b86775350598dea8efb421df45
# uuid-dev SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

# Exact FRR AMD64 validation inputs: libperl is a native dependency; uuid-dev
# ARM64 is imported during AMD64 sysroot analysis, without ARM execution.
# SHA1 locates the bytes; each original APT-lock SHA256 remains authoritative.
# libperl5.40 SHA256: f20aa427c3f56f430b7b58b73222edcb8cda40fa9c3b25365b20cd53eddd51a9
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/libperl5\.40_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/115f4cbe17c3706c068bab13e8659621c15591f2
# uuid-dev ARM64 SHA256: 142f2c809a9c239b94a1a09dccc02a49cc4b20990da687b60f849cda84dd580a
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_arm64\.deb https://snapshot.debian.org/file/cb5244c39df10c7ce2573dbf21944fe03d8a436b

# Eventd AMD64 tests use this exact private Redis package. Keep its locked bytes.
# SHA256: 0edc72458f66c68f2d4ccd0c9959a85a81f4ec7de410a28861cde4a2059f245c
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/file/c502f81818978156937bce72be28acac56cf8da6
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb

# These three security-package exceptions retain their exact original URLs.
# libc-ares-dev also has the same bytes available in the official security CDN.
# libc-ares-dev SHA256: 40d2336fb01e4b707ad8a6bfbad522fc7ddff3b057d16ee981237fb7cef5d95c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/109d7800bf370139268bea60e2ea7e2699cc3d48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
# openssl-provider-legacy SHA256: b099729f6a38d7d57f7f442f0f836c862673b9bb0ee65d757da6e3cdebbfd2a4
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/d5a41e70f8551fb076606ed0b1c0dab7fa9bc32d
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_arm64.deb
# openssl SHA256: 1ee735e2480519bd1fd5f6576a879243bc81b9773059e1fa1ec8b8943c84e77c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/4b411e66ee3d3106570389d5d1e022b15c1975a8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3.5.6-1~deb13u2_arm64.deb

# Retained exact historical inputs for declared tools and SONiC source images.
# Content IDs locate bytes; the original locked SHA256 remains authoritative.
# SHA256: ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 0def2d972310b59704ad119abee5a97f95409e14ff1359edd8cc7b8892cfd43f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-stdlib_3\.13\.5-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/34446b6d753038d30cc9dfb5bf17987ff56071da
# SHA256: 11ec93d44649e9b37e3bddcf32a5f01d6e9dd3d0d79aac621b49473539c8f5b8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/perl_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/9c5005df956b8fa8a3f487c175499e6029669465
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354
# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: fed7de7d14f7df765bb6ef1a79bd55b90475808793a5c41dc9ccb9deacf639ae
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif-plugin-dav1d_1\.19\.8-1_amd64\.deb https://snapshot.debian.org/file/4db82883543536a32eb80350a3ac4149d85009de
# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100
# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88
# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\.7\.1-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9
# SHA256: c98b1130f08ef6ac3932972c56be84cfc308ce1afa37105cb06aaf47ced2d078
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\.13-1_amd64\.deb https://snapshot.debian.org/file/17e2275e726dad554ab38aa3f3d240f5270923cb
# SHA256: 8ffd13165b9ee3f067e2ee670df718e48c1bdaa18676ac93d1de761dbbb3913c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/11449623c6859d9164dff8aedbcf023edaddc513
# SHA256: ff529924782d3286181188fc265a6a92e7fe28975fb3a925dc0e05c0ca66e52f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/8dd0cfbe0f5bd2c3923c8252c00c868bc6ac945e
# Exact packages whose dated URLs failed during the ARM cross-build survey.
# SHA256: 10247744737c1bb574f9667b3f06a3b43a5055c67bdfcc1945803451cc036db3
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libuuid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/69561687e45ee4833a59400e51976ff05cf71c0d
# SHA256: 1652525c1c605b2169b3b27beecd01cbaa0e5abd7ec4d2029cbae2084dbde012
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/x/xapian-core/libxapian30_1\.4\.29-3_arm64\.deb https://snapshot.debian.org/file/b3cef6d6dfd6511f04a77442026f81811313012e
# SHA256: fc167d04049dfcf91ccff8687222a186be681cac2298ae89cf8523518ad04c88
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/file/a6e3b3b45aaf7799d6ab6ad71e09218a813866c8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb
# SHA256: ff88e52a6b6599851f3d44f7223f15123d9e5a0b02ed9a308c05c31fa88f7d73
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/3e0610e3b6f6d57539344d7a1f95da1ed442fd48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb
# SHA256: bed93f11942524e5338074c46cce376197740635bf0745ccb7d259e0f6ce7117
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libblkid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/8413cb58799327072dd69a418e41fc05009cfd03
# SHA256: fd62da71db387aba934e4bd17cceaf0db1454ae9e1eae378c4d8329fbe856883
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-dev_3\.13\.5-2\+deb13u4_arm64\.deb https://snapshot.debian.org/file/f0907e7116933f0437ac671e9946e4740fcef10b
# SHA256: 6bb2038f36817ae1ee6d33df03917b21835dcc2e0f3e3e80a71390e83c171173
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/file/faa5bddd33fff3675e7b76863c6ef140347fb3b2
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_amd64.deb
# SHA256: 879e8477dc20006e59788831e4f3a436d15afe3c055ae0e93beb96696f0e5eb1
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/libss2_1\.47\.2-3\+b11_amd64\.deb https://snapshot.debian.org/file/458f436ed67e7f80aed736c6665c6b725301024c
# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb

# Exact Debian patch archive used by source-base-image socat@1.8.0.2.sonic.3.
# SHA256 04b361cd91884eda6ca9fe2890ca83a6abe73dd8ce75fe9927b2d8443af36123; the SHA1 URL is only a locator.
# All other source archives stay outside the generic binary-package rewrites.
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/file/0475522901bd85478b944520b23e01e3f3c7d339
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1.8.0.2-1.debian.tar.xz

# Exact sibling architectures from the same failed consumers' unchanged APT locks.
# SHA256: 04250d9e51fc87ef6d539f36cf0b66668dd34beec101af56dc35198ea99cef00
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/ed4549f189f5faa3929d51268c16419c2ee87616
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_amd64.deb
# SHA256: afdd9c57261502a3a434ba606ddfd1e8fd0f288c663f2dc4f7570ce927867651
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/e00e42b454c6acfd3a11622bc8aca25e8b4a232b
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_arm64.deb
# SHA256: b2dab3fe29c1fcb82ef58c2db0ff4bd830a442cf37b9a8b730b7b6ce87181349
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/fa81f709727b16de2a82710bbe0556b58f2b3f50
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_amd64.deb
# SHA256: f7e09a12ccb2d6bd28bcc87a6e2462a0558dcb3cc34d56f90ee86fbd3402d8ba
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/6e3fe344496850bb946e702685bacc5df1332dab
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb
# SHA256: b574fdbc1ac7a50df51f382a23847a68687892ceb06dcf0b1bc1ed4730109c28
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/file/70634fafccf8dee1bbccd0091a321e81f8983755
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_armhf.deb
# SHA256: f875f56675be5b074da877f9a93b09d47dc2eb4e679951d36e2943b8d4843344
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/2df60d569f5ba6703427c701e3707c04cd958a03
# SHA256: 7f6868227f4893a11123e43b9ed291950b550c7dc209905b2fb871c74678d1bf
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_arm64\.deb https://snapshot.debian.org/file/48585a6c3fab8508da2a2702d18a16f3c605b9ab
# SHA256: 1c82e8bb5d628ca8f8343975b26578d0894b8a1c33b54bc7741a6bfa8d880654
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_armhf\.deb https://snapshot.debian.org/file/565675e8541721cdfa011219b0eba6efebf6f2d7
# SHA256: dabada7ca12f95a17a31d19c3be4a8708a6a37207ac3b46cd1c344503825b0ee
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-dev_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/3cb20569f34ff9c1f8537215183f7ce639a343f7
# SHA256: 85b7d04605f9d423d5699b3c07f027e8380649fd164a51e0712cded0cc56eced
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_arm64\.deb https://snapshot.debian.org/file/0cc7ca3341253904256e5dc4296786ce151e51eb
# SHA256: 1a4b6e94cbc26e03e791f7a6e5ae714adc03ee5fe5b24c01651d1194d9de3a3c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_armhf\.deb https://snapshot.debian.org/file/5563fcda49878ea86b02546db7c47c42af9a0a72

# Timestamped Debian binary packages: same pool path on the official CDN first,
# then the exact snapshot URL. Bazel still checks the locked package SHA256.
# These generic rules exclude indexes, source archives, and debian-security.
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2

# Cloudflare snapshot package inputs retain CDN, standard snapshot, then original.
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot-cloudflare.debian.org/archive/debian/$1/$2
"""

def rewritten_urls(url):
    # Bazel 8.5.1 UrlRewriter.applyRewriteRules matches the complete URL without
    # its scheme, collecting every matching replacement in configuration order.
    # These data-only rules use the common Java/Python regex subset.
    candidate = url.split("://", 1)[1]
    result = []
    for line in (ROOT / CONFIG).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        directive, expression, replacement = line.split()
        if directive != "rewrite":
            raise AssertionError("unexpected downloader directive")
        match = re.fullmatch(expression, candidate)
        if match:
            result.append(re.sub(r"\$(\d+)", lambda group: match[int(group[1])], replacement))
    return list(dict.fromkeys(result)) or [url]


# Exact successor selected only by FRR native and Rust validation.
BASE_ARCHIVE_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# The three published gawk locations retain their original SHA256 and fallback.
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://mirrors.kernel.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftpmirror.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz

# Exact BCR ARM archive URLs only; preserve their SHA512 integrity and versions.
# Multiple matching rewrites are tried in order: verified GitHub copy, then upstream.
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/ape-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/ape/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/download_utils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/download_utils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_coreutils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_coreutils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_gzip-v1.0.0.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_gzip/-/releases/v1.0.0/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/toolchain_utils-v1.0.2.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/toolchain_utils/-/releases/v1.0.2/downloads/src.tar.gz

# This exact retired libcap-dev package has an available content-addressed URL
# even when Debian's dated archive path returns 503. SHA1 is only its URL locator;
# the original APT-lock SHA256 remains authoritative:
# cabb54ec097f89dd130902689a6bf38a67782fbf2aeca5f226ea656468843ec7
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap-dev_2\.75-10\+deb13u1\+b1_amd64\.deb https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6

# Preserve the retired libaudit-dev package through Debian's content address.
# Original APT-lock SHA256:
# 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524

# Exact ARM64 packages mirrored byte-for-byte after snapshot download failures.
# Original APT-lock SHA256 for libcap2:
# c9a77aa670c9ca163c624a00e55c4419df40b770fe516010dc495c04a16c80b0
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75-10\+deb13u1\+b1_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_arm64.deb
# Original APT-lock SHA256 for Bash:
# f9b0e4eb4ae81b3934a3211d755de26fa03881af3f159eb582437d3e8f55d9eb
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/b/bash/bash_5\.2\.37-2\+b9_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/bash_5.2.37-2%2Bb9_arm64.deb

# Exact packages selected by native ARM CI, using verified Debian content URLs.
# The SHA1 locates the file; each original APT-lock SHA256 below is unchanged.
# libc-bin SHA256: 02f366115bea79b87fe26c7dec4dc444f9fdcc76440905d88cc26dbef075511b
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc-bin_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/2f97c88e63e698b86775350598dea8efb421df45
# uuid-dev SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

# Exact FRR AMD64 validation inputs: libperl is a native dependency; uuid-dev
# ARM64 is imported during AMD64 sysroot analysis, without ARM execution.
# SHA1 locates the bytes; each original APT-lock SHA256 remains authoritative.
# libperl5.40 SHA256: f20aa427c3f56f430b7b58b73222edcb8cda40fa9c3b25365b20cd53eddd51a9
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/libperl5\.40_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/115f4cbe17c3706c068bab13e8659621c15591f2
# uuid-dev ARM64 SHA256: 142f2c809a9c239b94a1a09dccc02a49cc4b20990da687b60f849cda84dd580a
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_arm64\.deb https://snapshot.debian.org/file/cb5244c39df10c7ce2573dbf21944fe03d8a436b

# Eventd AMD64 tests use this exact private Redis package. Keep its locked bytes.
# SHA256: 0edc72458f66c68f2d4ccd0c9959a85a81f4ec7de410a28861cde4a2059f245c
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/file/c502f81818978156937bce72be28acac56cf8da6
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb

# These three security-package exceptions retain their exact original URLs.
# libc-ares-dev also has the same bytes available in the official security CDN.
# libc-ares-dev SHA256: 40d2336fb01e4b707ad8a6bfbad522fc7ddff3b057d16ee981237fb7cef5d95c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/109d7800bf370139268bea60e2ea7e2699cc3d48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
# openssl-provider-legacy SHA256: b099729f6a38d7d57f7f442f0f836c862673b9bb0ee65d757da6e3cdebbfd2a4
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/d5a41e70f8551fb076606ed0b1c0dab7fa9bc32d
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_arm64.deb
# openssl SHA256: 1ee735e2480519bd1fd5f6576a879243bc81b9773059e1fa1ec8b8943c84e77c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/4b411e66ee3d3106570389d5d1e022b15c1975a8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3.5.6-1~deb13u2_arm64.deb

# Retained exact historical inputs for declared tools and SONiC source images.
# Content IDs locate bytes; the original locked SHA256 remains authoritative.
# SHA256: ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 0def2d972310b59704ad119abee5a97f95409e14ff1359edd8cc7b8892cfd43f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-stdlib_3\.13\.5-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/34446b6d753038d30cc9dfb5bf17987ff56071da
# SHA256: 11ec93d44649e9b37e3bddcf32a5f01d6e9dd3d0d79aac621b49473539c8f5b8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/perl_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/9c5005df956b8fa8a3f487c175499e6029669465
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354
# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: fed7de7d14f7df765bb6ef1a79bd55b90475808793a5c41dc9ccb9deacf639ae
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif-plugin-dav1d_1\.19\.8-1_amd64\.deb https://snapshot.debian.org/file/4db82883543536a32eb80350a3ac4149d85009de
# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100
# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88
# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\.7\.1-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9
# SHA256: c98b1130f08ef6ac3932972c56be84cfc308ce1afa37105cb06aaf47ced2d078
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\.13-1_amd64\.deb https://snapshot.debian.org/file/17e2275e726dad554ab38aa3f3d240f5270923cb
# SHA256: 8ffd13165b9ee3f067e2ee670df718e48c1bdaa18676ac93d1de761dbbb3913c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/11449623c6859d9164dff8aedbcf023edaddc513
# SHA256: ff529924782d3286181188fc265a6a92e7fe28975fb3a925dc0e05c0ca66e52f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/8dd0cfbe0f5bd2c3923c8252c00c868bc6ac945e
# Exact packages whose dated URLs failed during the ARM cross-build survey.
# SHA256: 10247744737c1bb574f9667b3f06a3b43a5055c67bdfcc1945803451cc036db3
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libuuid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/69561687e45ee4833a59400e51976ff05cf71c0d
# SHA256: 1652525c1c605b2169b3b27beecd01cbaa0e5abd7ec4d2029cbae2084dbde012
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/x/xapian-core/libxapian30_1\.4\.29-3_arm64\.deb https://snapshot.debian.org/file/b3cef6d6dfd6511f04a77442026f81811313012e
# SHA256: fc167d04049dfcf91ccff8687222a186be681cac2298ae89cf8523518ad04c88
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/file/a6e3b3b45aaf7799d6ab6ad71e09218a813866c8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb
# SHA256: ff88e52a6b6599851f3d44f7223f15123d9e5a0b02ed9a308c05c31fa88f7d73
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/3e0610e3b6f6d57539344d7a1f95da1ed442fd48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb
# SHA256: bed93f11942524e5338074c46cce376197740635bf0745ccb7d259e0f6ce7117
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libblkid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/8413cb58799327072dd69a418e41fc05009cfd03
# SHA256: fd62da71db387aba934e4bd17cceaf0db1454ae9e1eae378c4d8329fbe856883
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-dev_3\.13\.5-2\+deb13u4_arm64\.deb https://snapshot.debian.org/file/f0907e7116933f0437ac671e9946e4740fcef10b
# SHA256: 6bb2038f36817ae1ee6d33df03917b21835dcc2e0f3e3e80a71390e83c171173
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/file/faa5bddd33fff3675e7b76863c6ef140347fb3b2
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_amd64.deb
# SHA256: 879e8477dc20006e59788831e4f3a436d15afe3c055ae0e93beb96696f0e5eb1
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/libss2_1\.47\.2-3\+b11_amd64\.deb https://snapshot.debian.org/file/458f436ed67e7f80aed736c6665c6b725301024c
# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb

# Exact Debian patch archive used by source-base-image socat@1.8.0.2.sonic.3.
# SHA256 04b361cd91884eda6ca9fe2890ca83a6abe73dd8ce75fe9927b2d8443af36123; the SHA1 URL is only a locator.
# All other source archives stay outside the generic binary-package rewrites.
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/file/0475522901bd85478b944520b23e01e3f3c7d339
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1.8.0.2-1.debian.tar.xz

# Exact sibling architectures from the same failed consumers' unchanged APT locks.
# SHA256: 04250d9e51fc87ef6d539f36cf0b66668dd34beec101af56dc35198ea99cef00
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/ed4549f189f5faa3929d51268c16419c2ee87616
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_amd64.deb
# SHA256: afdd9c57261502a3a434ba606ddfd1e8fd0f288c663f2dc4f7570ce927867651
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/e00e42b454c6acfd3a11622bc8aca25e8b4a232b
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_arm64.deb
# SHA256: b2dab3fe29c1fcb82ef58c2db0ff4bd830a442cf37b9a8b730b7b6ce87181349
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/fa81f709727b16de2a82710bbe0556b58f2b3f50
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_amd64.deb
# SHA256: f7e09a12ccb2d6bd28bcc87a6e2462a0558dcb3cc34d56f90ee86fbd3402d8ba
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/6e3fe344496850bb946e702685bacc5df1332dab
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb
# SHA256: b574fdbc1ac7a50df51f382a23847a68687892ceb06dcf0b1bc1ed4730109c28
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/file/70634fafccf8dee1bbccd0091a321e81f8983755
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_armhf.deb
# SHA256: f875f56675be5b074da877f9a93b09d47dc2eb4e679951d36e2943b8d4843344
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/2df60d569f5ba6703427c701e3707c04cd958a03
# SHA256: 7f6868227f4893a11123e43b9ed291950b550c7dc209905b2fb871c74678d1bf
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_arm64\.deb https://snapshot.debian.org/file/48585a6c3fab8508da2a2702d18a16f3c605b9ab
# SHA256: 1c82e8bb5d628ca8f8343975b26578d0894b8a1c33b54bc7741a6bfa8d880654
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_armhf\.deb https://snapshot.debian.org/file/565675e8541721cdfa011219b0eba6efebf6f2d7
# SHA256: dabada7ca12f95a17a31d19c3be4a8708a6a37207ac3b46cd1c344503825b0ee
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-dev_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/3cb20569f34ff9c1f8537215183f7ce639a343f7
# SHA256: 85b7d04605f9d423d5699b3c07f027e8380649fd164a51e0712cded0cc56eced
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_arm64\.deb https://snapshot.debian.org/file/0cc7ca3341253904256e5dc4296786ce151e51eb
# SHA256: 1a4b6e94cbc26e03e791f7a6e5ae714adc03ee5fe5b24c01651d1194d9de3a3c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_armhf\.deb https://snapshot.debian.org/file/5563fcda49878ea86b02546db7c47c42af9a0a72

# Observed PR195 source-base/native analysis inputs; exact immutable package identities only.
# SHA256: 62c3dbd746163878e73e22206d1dcc01f0f5296d3ca4ece5fb8cb7cb80396e4f; 16204 bytes.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/4fef5fafa9bcf0df24e4cc6ad971283da129ffa7
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5-dev_1.21.3-5+deb13u1_amd64.deb
# SHA256: 6aa452af6a07e34498bf8e734e8789c9dd602fb34f12572e62dc1bba4889a60e; 298536 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-8\-0_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/1594986c3db12e53c6d738df0926766b852e8e9d
# SHA256: b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef; 852668 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd
# SHA256: cf70657d734060bad45def0c67208e2afb1e165a5640d53b3bfe2264a3ada932; 750500 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_arm64\.deb https://snapshot.debian.org/file/f114a29adbd36f6c0b136ab62613226f066e1fbf
# SHA256: a9210d107c63761a88847c0fdfcb7b15bcf7cd1fc443b91a25e58805a6f4f83d; 756112 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_armhf\.deb https://snapshot.debian.org/file/e4ad4f9737d3906da5f6cdbb819176ee0ac0ad57

# Exact FreeType runtime dependencies from the unchanged sonic-build-infra .28 APT lock.
# SHA256: e4947f3291528f03d574f2b01d5c5fc45c58c47480f32888a5cdf0f231ab584e
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/file/2602e72ebeb8d5aa36eb1cc60f1cabfbd7558f43
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
# SHA256: 0c426e83f1af816b9a4df9056fd2238463d1cd07969ee640965da9fb6ecaf107
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/218fd3daa2391ba367cdd38ff4f8f79f6b3757e1
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb

# Exact rsyslog-relp input from the PR163 source-base AMD64 analysis failure.
# SHA256: 8b58d38a801e33182b5bf66382f012830e8cd8b234358f76657eb946741a87ae; 316252 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/r/rsyslog/rsyslog\-relp_8\.2504\.0\-1_amd64\.deb https://snapshot.debian.org/file/c16b59f13516c30140c256c1127b3be80899aff7

# Exact JQ and ss-dev inputs observed by AMD64 SWSS/FRR image analysis.
# SHA256: 140ac37e4b35370c3f34c95407cba75b35570e0be4bb9eebbf40bd72d182257c; 164336 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/libjq1_1\.7\.1\-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/b8b613de6336c793442ca724edcb30be97ee3de0
# SHA256: 6792fc1b77c4335e780cf93fe5830a7930b392af2be0577cf25cb583bb4c1d48; 34996 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/ss\-dev_2\.0\-1\.47\.2\-3\+b11_armhf\.deb https://snapshot.debian.org/file/e2267362c7557b71bcdc844929316ebfe8256dc3

# Timestamped Debian binary packages: same pool path on the official CDN first,
# then the exact snapshot URL. Bazel still checks the locked package SHA256.
# These generic rules exclude indexes, source archives, and debian-security.
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2

# Cloudflare snapshot package inputs retain CDN, standard snapshot, then original.
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot-cloudflare.debian.org/archive/debian/$1/$2
"""

FRR_RUST_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# The three published gawk locations retain their original SHA256 and fallback.
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite mirrors\.kernel\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://mirrors.kernel.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftp\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://distfiles.macports.org/gawk/gawk-5.3.2.tar.xz
rewrite ftpmirror\.gnu\.org/gnu/gawk/gawk-5\.3\.2\.tar\.xz https://ftpmirror.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz

# Exact BCR ARM archive URLs only; preserve their SHA512 integrity and versions.
# Multiple matching rewrites are tried in order: verified GitHub copy, then upstream.
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/ape-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/ape/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/ape/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/download_utils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/download_utils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/download_utils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_coreutils-v1.0.1.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_coreutils/-/releases/v1\.0\.1/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_coreutils/-/releases/v1.0.1/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/rules_gzip-v1.0.0.tar.gz
rewrite gitlab\.arm\.com/bazel/rules_gzip/-/releases/v1\.0\.0/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/rules_gzip/-/releases/v1.0.0/downloads/src.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/toolchain_utils-v1.0.2.tar.gz
rewrite gitlab\.arm\.com/bazel/toolchain_utils/-/releases/v1\.0\.2/downloads/src\.tar\.gz https://gitlab.arm.com/bazel/toolchain_utils/-/releases/v1.0.2/downloads/src.tar.gz

# This exact retired libcap-dev package has an available content-addressed URL
# even when Debian's dated archive path returns 503. SHA1 is only its URL locator;
# the original APT-lock SHA256 remains authoritative:
# cabb54ec097f89dd130902689a6bf38a67782fbf2aeca5f226ea656468843ec7
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap-dev_2\.75-10\+deb13u1\+b1_amd64\.deb https://snapshot.debian.org/file/0f1157b480b41fa81255ebfe58b11b0767c5bbc6

# Preserve the retired libaudit-dev package through Debian's content address.
# Original APT-lock SHA256:
# 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524

# Exact ARM64 packages mirrored byte-for-byte after snapshot download failures.
# Original APT-lock SHA256 for libcap2:
# c9a77aa670c9ca163c624a00e55c4419df40b770fe516010dc495c04a16c80b0
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75-10\+deb13u1\+b1_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_arm64.deb
# Original APT-lock SHA256 for Bash:
# f9b0e4eb4ae81b3934a3211d755de26fa03881af3f159eb582437d3e8f55d9eb
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/b/bash/bash_5\.2\.37-2\+b9_arm64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/bash_5.2.37-2%2Bb9_arm64.deb

# Exact retained Debian packages mirrored byte-for-byte after upstream outages.
# Original APT-lock SHA256: ec6c7a6770d1ff5295cf27b61436ec61b31d14bace3bff4fff336e3008818124; 408140 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap\-dev_2\.75\-10\+deb13u1\+b1_armhf\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap-dev_2.75-10%2Bdeb13u1%2Bb1_armhf.deb
# Original APT-lock SHA256: edeacbdd6a86ee82d314dccc180b740905fd7f6392b72e5c0533079fda449361; 914268 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/s/sqlite3/libsqlite3\-0_3\.46\.1\-7\+deb13u1_amd64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libsqlite3-0_3.46.1-7%2Bdeb13u1_amd64.deb

# Exact retained PCRE runtime imported during PR196 AMD64 Orchagent analysis.
# SHA256: fde8f26dfcc7073516c745381cf9940c964ce22e7458826dae5946b196c6115f; 259492 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-8\-0_10\.46\-1\~deb13u1_armhf\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libpcre2-8-0_10.46-1.deb13u1_armhf.deb


# Exact libcap2 AMD64 runtime selected by the current PR163 image lock.
# Original APT-lock SHA256: 860db6f80d400239744eee2b454f1d8e66b064e18dd0527535824887691e9cb7; 28988 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap2_2\.75\-10\+deb13u1\+b1_amd64\.deb https://github.com/securely1g/sonic-bazel-registry/releases/download/ci-source-mirrors-20261104/libcap2_2.75-10%2Bdeb13u1%2Bb1_amd64.deb

# Exact packages selected by native ARM CI, using verified Debian content URLs.
# The SHA1 locates the file; each original APT-lock SHA256 below is unchanged.
# libc-bin SHA256: 02f366115bea79b87fe26c7dec4dc444f9fdcc76440905d88cc26dbef075511b
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc-bin_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/2f97c88e63e698b86775350598dea8efb421df45
# uuid-dev SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

# Exact FRR AMD64 validation inputs: libperl is a native dependency; uuid-dev
# ARM64 is imported during AMD64 sysroot analysis, without ARM execution.
# SHA1 locates the bytes; each original APT-lock SHA256 remains authoritative.
# libperl5.40 SHA256: f20aa427c3f56f430b7b58b73222edcb8cda40fa9c3b25365b20cd53eddd51a9
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/libperl5\.40_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/115f4cbe17c3706c068bab13e8659621c15591f2
# uuid-dev ARM64 SHA256: 142f2c809a9c239b94a1a09dccc02a49cc4b20990da687b60f849cda84dd580a
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41-5_arm64\.deb https://snapshot.debian.org/file/cb5244c39df10c7ce2573dbf21944fe03d8a436b

# Eventd AMD64 tests use this exact private Redis package. Keep its locked bytes.
# SHA256: 0edc72458f66c68f2d4ccd0c9959a85a81f4ec7de410a28861cde4a2059f245c
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/file/c502f81818978156937bce72be28acac56cf8da6
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/r/redis/redis\-server_8\.0\.2\-3\+deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/r/redis/redis-server_8.0.2-3+deb13u2_amd64.deb

# These three security-package exceptions retain their exact original URLs.
# libc-ares-dev also has the same bytes available in the official security CDN.
# libc-ares-dev SHA256: 40d2336fb01e4b707ad8a6bfbad522fc7ddff3b057d16ee981237fb7cef5d95c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/109d7800bf370139268bea60e2ea7e2699cc3d48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
# openssl-provider-legacy SHA256: b099729f6a38d7d57f7f442f0f836c862673b9bb0ee65d757da6e3cdebbfd2a4
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/d5a41e70f8551fb076606ed0b1c0dab7fa9bc32d
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_arm64.deb
# openssl SHA256: 1ee735e2480519bd1fd5f6576a879243bc81b9773059e1fa1ec8b8943c84e77c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/4b411e66ee3d3106570389d5d1e022b15c1975a8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3.5.6-1~deb13u2_arm64.deb

# Retained exact historical inputs for declared tools and SONiC source images.
# Content IDs locate bytes; the original locked SHA256 remains authoritative.
# SHA256: ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.1.177-1_amd64.deb
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 0def2d972310b59704ad119abee5a97f95409e14ff1359edd8cc7b8892cfd43f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-stdlib_3\.13\.5-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/34446b6d753038d30cc9dfb5bf17987ff56071da
# SHA256: 11ec93d44649e9b37e3bddcf32a5f01d6e9dd3d0d79aac621b49473539c8f5b8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/perl/perl_5\.40\.1-6_amd64\.deb https://snapshot.debian.org/file/9c5005df956b8fa8a3f487c175499e6029669465
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354
# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: fed7de7d14f7df765bb6ef1a79bd55b90475808793a5c41dc9ccb9deacf639ae
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif-plugin-dav1d_1\.19\.8-1_amd64\.deb https://snapshot.debian.org/file/4db82883543536a32eb80350a3ac4149d85009de
# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100
# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88
# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\.7\.1-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9
# SHA256: c98b1130f08ef6ac3932972c56be84cfc308ce1afa37105cb06aaf47ced2d078
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\.13-1_amd64\.deb https://snapshot.debian.org/file/17e2275e726dad554ab38aa3f3d240f5270923cb
# SHA256: 8ffd13165b9ee3f067e2ee670df718e48c1bdaa18676ac93d1de761dbbb3913c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/11449623c6859d9164dff8aedbcf023edaddc513
# SHA256: ff529924782d3286181188fc265a6a92e7fe28975fb3a925dc0e05c0ca66e52f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41-12\+deb13u3_arm64\.deb https://snapshot.debian.org/file/8dd0cfbe0f5bd2c3923c8252c00c868bc6ac945e
# Exact packages whose dated URLs failed during the ARM cross-build survey.
# SHA256: 10247744737c1bb574f9667b3f06a3b43a5055c67bdfcc1945803451cc036db3
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libuuid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/69561687e45ee4833a59400e51976ff05cf71c0d
# SHA256: 1652525c1c605b2169b3b27beecd01cbaa0e5abd7ec4d2029cbae2084dbde012
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/x/xapian-core/libxapian30_1\.4\.29-3_arm64\.deb https://snapshot.debian.org/file/b3cef6d6dfd6511f04a77442026f81811313012e
# SHA256: fc167d04049dfcf91ccff8687222a186be681cac2298ae89cf8523518ad04c88
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/file/a6e3b3b45aaf7799d6ab6ad71e09218a813866c8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3\.5\.6-1~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_armhf.deb
# SHA256: ff88e52a6b6599851f3d44f7223f15123d9e5a0b02ed9a308c05c31fa88f7d73
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/3e0610e3b6f6d57539344d7a1f95da1ed442fd48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1\.21\.3-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssrpc4t64_1.21.3-5+deb13u1_amd64.deb
# SHA256: bed93f11942524e5338074c46cce376197740635bf0745ccb7d259e0f6ce7117
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libblkid1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/8413cb58799327072dd69a418e41fc05009cfd03
# SHA256: fd62da71db387aba934e4bd17cceaf0db1454ae9e1eae378c4d8329fbe856883
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13-dev_3\.13\.5-2\+deb13u4_arm64\.deb https://snapshot.debian.org/file/f0907e7116933f0437ac671e9946e4740fcef10b
# SHA256: 6bb2038f36817ae1ee6d33df03917b21835dcc2e0f3e3e80a71390e83c171173
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/file/faa5bddd33fff3675e7b76863c6ef140347fb3b2
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_amd64.deb
# SHA256: 879e8477dc20006e59788831e4f3a436d15afe3c055ae0e93beb96696f0e5eb1
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/libss2_1\.47\.2-3\+b11_amd64\.deb https://snapshot.debian.org/file/458f436ed67e7f80aed736c6665c6b725301024c
# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://deb.debian.org/debian-security/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6.12.96-1_all.deb

# Exact Debian patch archive used by source-base-image socat@1.8.0.2.sonic.3.
# SHA256 04b361cd91884eda6ca9fe2890ca83a6abe73dd8ce75fe9927b2d8443af36123; the SHA1 URL is only a locator.
# All other source archives stay outside the generic binary-package rewrites.
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/file/0475522901bd85478b944520b23e01e3f3c7d339
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\-1\.debian\.tar\.xz https://snapshot.debian.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1.8.0.2-1.debian.tar.xz

# Exact sibling architectures from the same failed consumers' unchanged APT locks.
# SHA256: 04250d9e51fc87ef6d539f36cf0b66668dd34beec101af56dc35198ea99cef00
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/ed4549f189f5faa3929d51268c16419c2ee87616
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_amd64.deb
# SHA256: afdd9c57261502a3a434ba606ddfd1e8fd0f288c663f2dc4f7570ce927867651
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/e00e42b454c6acfd3a11622bc8aca25e8b4a232b
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl\-dev_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl-dev_3.5.6-1~deb13u2_arm64.deb
# SHA256: b2dab3fe29c1fcb82ef58c2db0ff4bd830a442cf37b9a8b730b7b6ce87181349
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/file/fa81f709727b16de2a82710bbe0556b58f2b3f50
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_amd64.deb
# SHA256: f7e09a12ccb2d6bd28bcc87a6e2462a0558dcb3cc34d56f90ee86fbd3402d8ba
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/file/6e3fe344496850bb946e702685bacc5df1332dab
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_arm64.deb
# SHA256: b574fdbc1ac7a50df51f382a23847a68687892ceb06dcf0b1bc1ed4730109c28
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/file/70634fafccf8dee1bbccd0091a321e81f8983755
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3\.5\.6\-1\~deb13u2_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/libssl3t64_3.5.6-1~deb13u2_armhf.deb
# SHA256: f875f56675be5b074da877f9a93b09d47dc2eb4e679951d36e2943b8d4843344
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/2df60d569f5ba6703427c701e3707c04cd958a03
# SHA256: 7f6868227f4893a11123e43b9ed291950b550c7dc209905b2fb871c74678d1bf
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_arm64\.deb https://snapshot.debian.org/file/48585a6c3fab8508da2a2702d18a16f3c605b9ab
# SHA256: 1c82e8bb5d628ca8f8343975b26578d0894b8a1c33b54bc7741a6bfa8d880654
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1_2\.7\.1\-2_armhf\.deb https://snapshot.debian.org/file/565675e8541721cdfa011219b0eba6efebf6f2d7
# SHA256: dabada7ca12f95a17a31d19c3be4a8708a6a37207ac3b46cd1c344503825b0ee
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-dev_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/3cb20569f34ff9c1f8537215183f7ce639a343f7
# SHA256: 85b7d04605f9d423d5699b3c07f027e8380649fd164a51e0712cded0cc56eced
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_arm64\.deb https://snapshot.debian.org/file/0cc7ca3341253904256e5dc4296786ce151e51eb
# SHA256: 1a4b6e94cbc26e03e791f7a6e5ae714adc03ee5fe5b24c01651d1194d9de3a3c
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libuuid1_2\.41\-5_armhf\.deb https://snapshot.debian.org/file/5563fcda49878ea86b02546db7c47c42af9a0a72

# Observed PR195 source-base/native analysis inputs; exact immutable package identities only.
# SHA256: 62c3dbd746163878e73e22206d1dcc01f0f5296d3ca4ece5fb8cb7cb80396e4f; 16204 bytes.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/4fef5fafa9bcf0df24e4cc6ad971283da129ffa7
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-dev_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5-dev_1.21.3-5+deb13u1_amd64.deb
# SHA256: 6aa452af6a07e34498bf8e734e8789c9dd602fb34f12572e62dc1bba4889a60e; 298536 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-8\-0_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/1594986c3db12e53c6d738df0926766b852e8e9d
# SHA256: b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef; 852668 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd
# SHA256: cf70657d734060bad45def0c67208e2afb1e165a5640d53b3bfe2264a3ada932; 750500 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_arm64\.deb https://snapshot.debian.org/file/f114a29adbd36f6c0b136ab62613226f066e1fbf
# SHA256: a9210d107c63761a88847c0fdfcb7b15bcf7cd1fc443b91a25e58805a6f4f83d; 756112 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-dev_10\.46\-1\~deb13u1_armhf\.deb https://snapshot.debian.org/file/e4ad4f9737d3906da5f6cdbb819176ee0ac0ad57

# Exact FreeType runtime dependencies from the unchanged sonic-build-infra .28 APT lock.
# SHA256: e4947f3291528f03d574f2b01d5c5fc45c58c47480f32888a5cdf0f231ab584e
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/file/2602e72ebeb8d5aa36eb1cc60f1cabfbd7558f43
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_amd64.deb
# SHA256: 0c426e83f1af816b9a4df9056fd2238463d1cd07969ee640965da9fb6ecaf107
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/218fd3daa2391ba367cdd38ff4f8f79f6b3757e1
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2\.13\.3\+dfsg\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/f/freetype/libfreetype6_2.13.3+dfsg-1+deb13u1_arm64.deb

# Exact rsyslog-relp input from the PR163 source-base AMD64 analysis failure.
# SHA256: 8b58d38a801e33182b5bf66382f012830e8cd8b234358f76657eb946741a87ae; 316252 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/r/rsyslog/rsyslog\-relp_8\.2504\.0\-1_amd64\.deb https://snapshot.debian.org/file/c16b59f13516c30140c256c1127b3be80899aff7

# Exact JQ and ss-dev inputs observed by AMD64 SWSS/FRR image analysis.
# SHA256: 140ac37e4b35370c3f34c95407cba75b35570e0be4bb9eebbf40bd72d182257c; 164336 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/libjq1_1\.7\.1\-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/b8b613de6336c793442ca724edcb30be97ee3de0
# SHA256: 6792fc1b77c4335e780cf93fe5830a7930b392af2be0577cf25cb583bb4c1d48; 34996 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/ss\-dev_2\.0\-1\.47\.2\-3\+b11_armhf\.deb https://snapshot.debian.org/file/e2267362c7557b71bcdc844929316ebfe8256dc3

# Exact libexpat development input from SWSS AMD64 Python-target analysis.
# SHA256: ac1e7f8ad81578b61bc7c54c725ec639b06684e560766eac278490a1b2064372; 160568 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1\-dev_2\.7\.1\-2_amd64\.deb https://snapshot.debian.org/file/d718845c00344d7965787ca08877a6d1bc51ef71

# Exact warning inputs observed alongside the fatal SWSS libexpat download.
# SHA256: 3a0acd8b37955c0e102c756b52c97df2a31f67b96453c35dab70df218d309117; 33124 bytes.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/fdcbf424c12036172b67ed2d13d14fa441fb3736
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5support0_1.21.3-5+deb13u1_amd64.deb
# SHA256: 490511f32afe59303e1fa2b4c54fd174de0d98f5f3cf9702e0f2784b834792e4; 56944 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/comerr\-dev_2\.1\-1\.47\.2\-3\+b11_arm64\.deb https://snapshot.debian.org/file/9ba5541a96f7de01c0d343e78296b07e2672051f

# Exact ARM64 runtimes selected by native PI and P4C dependency analysis.
# SHA256: 2e791f8faf7bbbc94d354cf7b6c3728dc303b7aa327dfe7a2aa6954320977c8f; 198880 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_arm64\.deb https://snapshot.debian.org/file/81aee071e0a7fff9504c0ec2cbf6acf665ebf0bb
# SHA256: 208e9fdc14ed88a30f31fdfb993d1e8ab19f9ab29f3cd8546ffb4ea3f74f5a3f; 325232 bytes.
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://snapshot.debian.org/file/5e4fec5c53e730725b92a01b3e6663878562fa79
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4\.7\.0-3\+deb13u3_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/t/tiff/libtiff6_4.7.0-3+deb13u3_arm64.deb

# Exact libcap development input selected during Framework AMD64 analysis.
# Verified official content and accepted lock/cache: 470800 bytes; SHA256
# f1c0687bb0f6ad192b9cbe64429fc76029ce2a11939b155051139caf1683b447.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libc/libcap2/libcap\-dev_2\.75\-10\+deb13u1\+b1_arm64\.deb https://snapshot.debian.org/file/a5c6a45aae0d443be8f13ebc6eddfb3e83e23b55

# Exact rsyslog runtime package selected by the shared Sysmgr source image.
# Current checked-in lock and verified official content: 758296 bytes; SHA256
# 815203243b9eb17c72c0229fe26afb525542b867719ab36413ab8e514159a26b.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/r/rsyslog/rsyslog_8\.2504\.0\-1_amd64\.deb https://snapshot.debian.org/file/3745d404bae6227eb2142ce4aa8baeb8b0cad14d

# Exact dated package index selected by the existing native APT resolver.
# Retained same-URL fact and official content bytes: 4424 bytes; SHA256
# fe771d58d4af39dbb94142d6f3ce578d340c6b1364c172d9e87f2287bdc4994b.
# SHA1 is only the content locator, not a Release-signature assertion.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/dists/trixie\-updates/main/binary\-arm64/Packages\.xz https://snapshot.debian.org/file/c9d85a145fcc34817defe65c818ded141ea2c95f
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/dists/trixie\-updates/main/binary\-arm64/Packages\.xz https://snapshot.debian.org/archive/debian/20260727T143429Z/dists/trixie-updates/main/binary-arm64/Packages.xz

# Exact security package selected by the existing FRR AMD64 build tools.
# Official CDN bytes match the retained accepted lock: 843104 bytes; SHA256
# f0eb41f8cb40896e0822e9ab65611e8383c12b721c76c54764f744abb4a8c247.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libg/libgcrypt20/libgcrypt20_1\.11\.0\-7\+deb13u1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/libg/libgcrypt20/libgcrypt20_1.11.0-7+deb13u1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libg/libgcrypt20/libgcrypt20_1\.11\.0\-7\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/libg/libgcrypt20/libgcrypt20_1.11.0-7+deb13u1_amd64.deb

# Timestamped Debian binary packages: same pool path on the official CDN first,
# then the exact snapshot URL. Bazel still checks the locked package SHA256.
# These generic rules exclude indexes, source archives, and debian-security.
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2

# Cloudflare snapshot package inputs retain CDN, standard snapshot, then original.
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot-cloudflare.debian.org/archive/debian/$1/$2
"""


class DebianPoolTest(unittest.TestCase):
    def test_real_locked_packages_keep_the_original_as_second_choice(self):
        examples = [
            ("debian", "20260727T143429Z", "main/a/arping/arping_2.25-1_amd64.deb"),
            ("debian", "20260727T143429Z", "main/n/nlohmann-json3/nlohmann-json3-dev_3.11.3-2.1_all.deb"),
            ("debian", "20260727T143429Z", "main/libn/libnsl/libnsl-dev_1.3.0-3+b3_amd64.deb"),
            ("debian", "20260727T143429Z", "main/libf/libfido2/libfido2-dev_1.15.0-1+b1_armhf.deb"),
            ("debian", "20260727T143429Z", "main/r/readline/libreadline-dev_8.2-6_amd64.deb"),
            ("debian", "20260727T143429Z", "main/p/python-typing-extensions/python3-typing-extensions_4.13.2-1_all.deb"),
            ("debian-security", "20260726T121236Z", "updates/main/libs/libsodium/libsodium-dev_1.0.18-1+deb13u1_arm64.deb"),
        ]
        for archive, timestamp, path in examples:
            original = f"https://snapshot.debian.org/archive/{archive}/{timestamp}/pool/{path}"
            with self.subTest(package=path):
                self.assertEqual(rewritten_urls(original), [f"https://deb.debian.org/{archive}/pool/{path}", original])

    def test_indexes_source_archives_and_unrelated_urls_are_unchanged(self):
        prefix = "https://snapshot.debian.org/archive/debian/20260727T143429Z/"
        urls = [
            prefix + "dists/trixie/main/binary-amd64/Packages.xz",
            prefix + "dists/trixie/Release", prefix + "dists/trixie/InRelease",
            prefix + "pool/main/a/arping/arping_2.25.orig.tar.gz",
            prefix + "pool/main/a/arping/arping_2.25-1.dsc",
            prefix + "pool/main/a/arping/arping_2.25-1_amd64.deb?download=1",
            prefix + "pool/main/a/arping/arping_2.25-1_amd64.deb#fragment",
            prefix.replace("20260727T143429Z", "latest") + "pool/a.deb",
            prefix.replace("20260727T143429Z", "20260727") + "pool/a.deb",
            prefix.replace("snapshot.debian.org", "snapshot.debian.org.example") + "pool/a.deb",
            prefix.replace("snapshot.debian.org", "other.example") + "pool/a.deb",
            "https://deb.debian.org/debian/pool/main/a/arping/arping_2.25-1_amd64.deb",
            "https://snapshot.debian.org/archive/debian-security/20260726T121236Z/dists/trixie-security/main/binary-arm64/Packages.xz",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(rewritten_urls(url), [url])

    def test_configuration_only_changes_payload_transport(self):
        rules = [line.split() for line in (ROOT / CONFIG).read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(rules), 4)
        for directive, expression, replacement in rules:
            self.assertEqual(directive, "rewrite")
            self.assertTrue(expression.endswith(r"\.deb))"))
            self.assertTrue(replacement.startswith("https://"))
            self.assertIn(urlsplit(replacement).netloc, ("deb.debian.org", "$1"))

    def test_shared_default_keeps_local_override_and_cache_options(self):
        root_rc = (ROOT / ".bazelrc").read_text().splitlines()
        self.assertIn("import %workspace%/tools/bazel/common.bazelrc", root_rc)
        lines = (ROOT / "tools/bazel/common.bazelrc").read_text().splitlines()
        option = "common --experimental_downloader_config=" + CONFIG
        self.assertEqual(lines.count(option), 1)
        self.assertLess(lines.index(option), lines.index("try-import %workspace%/.bazelrc.user"))
        self.assertFalse(Path(CONFIG).is_absolute())
        self.assertTrue((ROOT / CONFIG).is_file())
        # No cache location is replaced by the shared default.
        for line in lines:
            self.assertFalse(any(token.startswith(("--disk_cache", "--repository_cache", "--distdir"))
                                 for token in shlex.split(line, comments=True)))

    def mirror_steps(self):
        found = []
        for name, expected in (("bazel-base-images.yml", 1), ("bazel-oci.yml", 2),
                               ("bazel-eventd.yml", 1), ("bazel-frr.yml", 1),
                               ("bazel-supervisor-rust.yml", 1)):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            steps = [(job_name, step["run"]) for job_name, job in workflow["jobs"].items()
                     for step in job.get("steps", [])
                     if step.get("name") == "Configure the verified source mirror"]
            self.assertEqual(len(steps), expected)
            if name == "bazel-supervisor-rust.yml":
                native_steps = workflow["jobs"]["native"]["steps"]
                names = [step.get("name") for step in native_steps]
                self.assertLess(names.index("Preserve setup-bazel caches for isolated CI"),
                                names.index("Configure the verified source mirror"))
                self.assertLess(names.index("Configure the verified source mirror"),
                                names.index("Audit and test source runtime and debug packages"))
            found.extend((name, job_name, script) for job_name, script in steps)
        return found

    def mirror_bytes(self, workflow=None, job=None):
        # Keep each workflow on its separately verified immutable transport input.
        if workflow == "bazel-supervisor-rust.yml":
            data = RUST_ARCHIVE_CONFIG_FIXTURE.encode()
            expected = "8cc37c150bfdac99dcddb0a3a22602fc500f5813b75bad19b3bf243668552a95"
        elif workflow == "bazel-frr.yml" or (workflow == "bazel-oci.yml" and job == "archive"):
            data = FRR_RUST_CONFIG_FIXTURE.encode()
            expected = "cf1b7d525e37bad86457b37e056f9eb1201baa2edc2beb7057e2f5667f203190"
        elif workflow == "bazel-base-images.yml":
            data = BASE_ARCHIVE_CONFIG_FIXTURE.encode()
            expected = "354c7d6ea5931c8ecb1c4a96fafa1269802b1547bde3b3b64dfc55a89a35cb46"
        elif workflow == "bazel-eventd.yml":
            data = SOURCE_IMAGE_CONFIG_FIXTURE.encode()
            expected = "cea2192b4b562c4dd5c0a42535164560d910253a00a60c298bba7e6f12a5e186"
        else:
            data = SHARED_CONFIG_FIXTURE.encode()
            expected = "9fbb19c33cc9564d439a86dc36e0833244ccb57cfc1511ac35a2923f908dfbc9"
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        return data

    def test_workflow_overrides_preserve_pigz_and_pool_transport(self):
        for name, job, script in self.mirror_steps():
            data = self.mirror_bytes(name, job)
            if name == "bazel-supervisor-rust.yml":
                revision = "f77605f0e9717395b7cae85e4a719035ec456c84"
            elif name == "bazel-frr.yml" or (name == "bazel-oci.yml" and job == "archive"):
                revision = "81864fd28fa2a843086e881f0ef033d9abfc6f5e"
            elif name == "bazel-base-images.yml":
                revision = "a32c87f0658e20f5fecd6459ef006caed96d905f"
            elif name == "bazel-eventd.yml":
                revision = "45065949f5163bd5d99793026b076eafec213ef8"
            else:
                revision = "b7b92bcfa5857685ee7cd7953fdbf55264989932"
            url = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + revision + "/ci/source_mirrors.cfg"
            with self.subTest(workflow=name, job=job), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = "common --repository_cache=/retained/repository\ncommon --disk_cache=/retained/disk\ncommon --distdir=/retained/downloads\n"
                code = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
                with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                    Path(".bazelrc.user").write_text(before)
                    prefix = b""
                    if name in ("bazel-base-images.yml", "bazel-eventd.yml", "bazel-oci.yml"):
                        mirrors = Path("tools/bazel/debian_mirrors.cfg")
                        mirrors.parent.mkdir(parents=True)
                        mirrors.write_bytes((ROOT / mirrors).read_bytes())
                        prefix = mirrors.read_bytes() + b"\n"
                    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as fetch:
                        exec(compile(code, name, "exec"), {})
                    fetch.assert_called_once_with(url, timeout=60)
                    destination = root / "sonic-source-mirrors.cfg"
                    self.assertEqual(destination.read_bytes(), prefix + data)
                    self.assertEqual(Path(".bazelrc.user").read_text(), before +
                                     "common --experimental_downloader_config=" + str(destination) + "\n")

    def test_corrupt_transport_leaves_existing_config_and_cache_options_untouched(self):
        for name, job, script in self.mirror_steps():
            with self.subTest(workflow=name, job=job), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                code = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
                with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                    Path(".bazelrc.user").write_text("common --disk_cache=/retained/cache\n")
                    destination = root / "sonic-source-mirrors.cfg"
                    destination.write_bytes(b"original verified configuration")
                    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"corrupt download")):
                        with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                            exec(compile(code, name, "exec"), {})
                    self.assertEqual(destination.read_bytes(), b"original verified configuration")
                    self.assertEqual(Path(".bazelrc.user").read_text(), "common --disk_cache=/retained/cache\n")
                    if name in ("bazel-base-images.yml", "bazel-eventd.yml", "bazel-oci.yml"):
                        data = self.mirror_bytes(name, job)
                        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                            with self.assertRaises(FileNotFoundError):
                                exec(compile(code, name, "exec"), {})
                        self.assertEqual(destination.read_bytes(), b"original verified configuration")
                        self.assertEqual(Path(".bazelrc.user").read_text(),
                                         "common --disk_cache=/retained/cache\n")

    def test_native_and_draft_callers_load_the_shared_rc_explicitly(self):
        # Execute the existing shell interfaces against an argv recorder, never Bazel.
        wrappers = list((ROOT / "tools/bazel/ci").glob("*_draft_bazel.sh"))
        self.assertEqual(len(wrappers), 1)
        owner = wrappers[0].name.removesuffix("_draft_bazel.sh")
        workflow = yaml.safe_load((ROOT / ".github/workflows" / ("bazel-" + owner + ".yml")).read_text())
        scripts = [step["run"] for job in workflow["jobs"].values() for step in job.get("steps", [])
                   if "run_bazel() {" in step.get("run", "")]
        self.assertEqual(len(scripts), 1)
        function = re.search(r"(?ms)^run_bazel\(\) \{\n.*?^\}", scripts[0]).group(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = root / "bazel"
            recorder.write_text("#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            recorder.chmod(0o755)
            rc = root / "tools/bazel/common.bazelrc"
            rc.parent.mkdir(parents=True)
            rc.write_text((ROOT / "tools/bazel/common.bazelrc").read_text())
            (root / CONFIG).write_bytes((ROOT / CONFIG).read_bytes())
            env = {"PATH": str(root) + ":/usr/bin:/bin"}
            commands = [["/bin/bash", str(wrappers[0]), "cquery", "//:transport_probe"],
                        ["/bin/bash", "-eu", "-c", "options=(--jobs=2)\n" + function +
                         "\nrun_bazel cquery //:transport_probe\n"]]
            for command in commands:
                result = subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True)
                argv = json.loads(result.stdout)
                self.assertIn("--nohome_rc", argv)
                self.assertIn("--noworkspace_rc", argv)
                self.assertIn("--bazelrc=" + str(rc), argv)
                self.assertIn("cquery", argv)
                self.assertFalse(any(arg.startswith("--experimental_downloader_config=") for arg in argv))
                self.assertLess(rc.read_text().index("--experimental_downloader_config="),
                                rc.read_text().index("try-import %workspace%/.bazelrc.user"))
                self.assertTrue((root / CONFIG).is_file())

    def test_fresh_snapshot_keeps_the_shared_config_and_kernel_stays_independent(self):
        with mock.patch.object(sys, "path", [str(ROOT), *sys.path]):
            from tools.bazel.ci.verify_agent_cache import snapshot
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            names = (".bazelrc", "tools/bazel/common.bazelrc", CONFIG)
            for name in names:
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / name).read_bytes())
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            copied = Path(directory) / "snapshot"
            snapshot(source, copied)
            for name in names:
                self.assertEqual((source / name).read_bytes(), (copied / name).read_bytes())
        # The retained kernel consumer copies its independent template directory;
        # it does not inherit this root's common rc or its relative config path.
        kernel_rc = (ROOT / "tools/bazel/kernel/.bazelrc").read_text()
        self.assertNotIn("common.bazelrc", kernel_rc)
        self.assertNotIn("debian-pool.cfg", kernel_rc)
        self.assertIn('shutil.copytree(workspace / "tools/bazel/kernel", consumer)',
                      (ROOT / "tools/bazel/kernel/prepare.py").read_text())

    def test_ci_runs_this_regression_with_explicit_yaml_dependency(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        callers = []
        for job in workflow["jobs"].values():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                script = step.get("run", "")
                if "for test in tools/bazel/tests/*_test.py; do" not in script:
                    continue
                callers.append(step["name"])
                setup = [prior["run"] for prior in steps[:index]
                         if prior.get("name") == "Install execution tools"]
                self.assertEqual(len(setup), 1)
                self.assertIn("python3-yaml", shlex.split(setup[0]))
                # Exercise both real CI selectors with only this regression's
                # pathname present, using an argv recorder instead of Python.
                loop = script[:script.index("\nfi") + len("\nfi")]
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    test = root / "tools/bazel/tests/debian_pool_test.py"
                    test.parent.mkdir(parents=True)
                    test.write_text("")
                    recorder = root / "python3"
                    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
                    recorder.chmod(0o755)
                    for source_only in ("true", "false"):
                        result = subprocess.run(["bash", "-eu", "-c", loop], cwd=root,
                                                env={"PATH": str(root) + ":/usr/bin:/bin",
                                                     "SOURCE_ONLY_NO_DEBS": source_only},
                                                check=True, capture_output=True, text=True)
                        expected = ("-B tools/bazel/tests/debian_pool_test.py" if source_only == "true"
                                    else "-B -m unittest discover -s tools/bazel/tests -p *_test.py")
                        self.assertEqual(result.stdout.strip(), expected)
        self.assertEqual(len(callers), 2)


class YangSourceMirrorTest(unittest.TestCase):
    def workflow_steps(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-yang-models.yml").read_text())
        steps = workflow["jobs"]["package"]["steps"]
        mirror = next(step for step in steps if step.get("name") == "Configure the verified source mirror")
        build = next(step for step in steps if step.get("name") == "Build and verify the installed source distribution")
        self.assertLess(steps.index(mirror), steps.index(build))
        return mirror, build

    def mirror_bytes(self):
        # Exact shared configuration with the observed YANG sysroot fallback.
        marker = r"rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1"
        addition = (
            "# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465\n"
            r"rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100"
            "\n\n"
        )
        data = SHARED_CONFIG_FIXTURE.replace("\n\n" + marker, "\n" + addition + marker, 1).encode()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "b6b7dbd03e20e482718de3f312a015168056f6c422d94723f55175a728259a7b")
        return data

    def configure(self, root, data):
        mirror, _ = self.workflow_steps()
        code = mirror["run"].split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
        with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as fetch:
                exec(compile(code, "yang-source-mirror", "exec"), {})
            fetch.assert_called_once_with(
                "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
                "6e7b45d19c76d479bc055d86ffd6fa2962948576/ci/source_mirrors.cfg", timeout=60)

    def test_actual_initial_query_loads_verified_mirror_without_changing_cache_options(self):
        data = self.mirror_bytes()
        _, build = self.workflow_steps()
        # Stop at the first query. Every Bazel call is an argv recorder.
        query = build["run"].split("git rev-parse HEAD", 1)[0]
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = "common --repository_cache=/retained/repository\ncommon --disk_cache=/retained/disk\ncommon --distdir=/retained/downloads\n"
                (root / ".bazelrc.user").write_text(before)
                for name in ("tools/bazel/common.bazelrc", CONFIG, "tools/bazel/ci/frr_draft_bazel.sh"):
                    destination = root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((ROOT / name).read_bytes())
                (root / "tools/bazel/ci/frr_draft_bazel.sh").chmod(0o755)
                self.configure(root, data)
                config = root / "sonic-source-mirrors.cfg"
                self.assertEqual(config.read_bytes(), data)
                self.assertEqual((root / ".bazelrc.user").read_text(), before +
                                 "common --experimental_downloader_config=" + str(config) + "\n")
                self.assertIn("try-import %workspace%/.bazelrc.user",
                              (root / "tools/bazel/common.bazelrc").read_text())
                executables = {
                    "bazel": "#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n",
                    "git": "#!/bin/sh\nexit 0\n",
                    "uname": "#!/bin/sh\nprintf '%s\\n' " + machine + "\n",
                }
                for name, content in executables.items():
                    executable = root / name
                    executable.write_text(content)
                    executable.chmod(0o755)
                working = root / build["working-directory"]
                working.mkdir(parents=True)
                subprocess.run(["bash", "-eu", "-c", query.replace("${{ matrix.machine }}", machine)],
                               cwd=working, env={"PATH": str(root) + ":/usr/bin:/bin"},
                               check=True, capture_output=True, text=True)
                argv = json.loads((working / "artifacts/image-root-targets.txt").read_text())
                self.assertEqual(argv, [
                    "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
                    "--bazelrc=" + str(root / "tools/bazel/common.bazelrc"), "cquery",
                    "--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/640979c957d1ebf404a4ead6a233a390225e98ff",
                    "--registry=https://bcr.bazel.build", "--lockfile_mode=update", "--lockfile_mode=update",
                    "//src/sonic-yang-models:yang_models_package_test",
                ])

    def test_corrupt_yang_mirror_cannot_replace_config_or_cache_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = "common --disk_cache=/retained/cache\n"
            (root / ".bazelrc.user").write_text(before)
            config = root / "sonic-source-mirrors.cfg"
            config.write_bytes(b"previous verified config")
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                self.configure(root, b"corrupt download")
            self.assertEqual(config.read_bytes(), b"previous verified config")
            self.assertEqual((root / ".bazelrc.user").read_text(), before)


if __name__ == "__main__":
    unittest.main()
