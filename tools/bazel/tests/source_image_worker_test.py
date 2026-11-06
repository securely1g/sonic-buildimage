#!/usr/bin/env python3
"""Check fixed image dispatch and public artifact boundaries."""

import importlib.util
import copy
import io
import hashlib
import json
import os
import re
import inspect
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

SHARED_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
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

# These security-package exceptions retain their exact original URLs.
# libc-ares-dev also has the same bytes available in the official security CDN.
# libc-ares-dev AMD64 SHA256: 0270c69a1229fdb60e198e8ba84c8c38cdfd020009cfefee59ad253bcec26d44
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_amd64\.deb https://snapshot.debian.org/file/df85291570dde99d0e36b489aa0faeda720ec09b
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_amd64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_amd64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_amd64.deb
# libc-ares-dev ARM64 SHA256: 40d2336fb01e4b707ad8a6bfbad522fc7ddff3b057d16ee981237fb7cef5d95c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/109d7800bf370139268bea60e2ea7e2699cc3d48
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://deb.debian.org/debian-security/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1\.34\.5-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/c/c-ares/libc-ares-dev_1.34.5-1+deb13u1_arm64.deb
# openssl-provider-legacy SHA256: b099729f6a38d7d57f7f442f0f836c862673b9bb0ee65d757da6e3cdebbfd2a4
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/d5a41e70f8551fb076606ed0b1c0dab7fa9bc32d
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl-provider-legacy_3.5.6-1~deb13u2_arm64.deb
# openssl SHA256: 1ee735e2480519bd1fd5f6576a879243bc81b9773059e1fa1ec8b8943c84e77c
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/file/4b411e66ee3d3106570389d5d1e022b15c1975a8
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3\.5\.6-1~deb13u2_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/o/openssl/openssl_3.5.6-1~deb13u2_arm64.deb

# PR151 AMD64 owner-source query imports this exact ARMHF sysroot package.
# Original APT-lock SHA256: f9797e3c0b091a540c1d33b630ba965aab9b128cb73c3c28ed4271c9e83b9354; 58444 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc\-dev\-bin_2\.41\-12\+deb13u3_armhf\.deb https://snapshot.debian.org/file/7ea08392e98a52327fbe68d853f0444e1f8f676d/libc-dev-bin_2.41-12%2Bdeb13u3_armhf.deb

# Retained exact historical inputs for declared tools and SONiC source images.
# Content IDs locate bytes; the original locked SHA256 remains authoritative.
# Exact inputs that stopped PR151 Rust and source-base AMD64 analysis.
# SHA256: 2eb5db4cb16a42850ed9d3ca8b2b1aeda91dd00d248f6068f49d641321748308; 59836 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc-dev-bin_2\.41-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/4a63880454e8cdb1c0fb13d23f472d8244039aad
# SHA256: 3a5d421b22e2e0495ceaa5ed130dd6f4a35578fbf333873a7f79e2e115c11f61; 84264 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2-2\+b2_armhf\.deb https://snapshot.debian.org/file/dc490b3ef1fd8c6f403b161ae66e52dcf3eb0302
# A sibling source-base warning, distinct from its fatal libaudit input.
# SHA256: 76c50d945fc5dd8d79fdfd6b63d03149689327f4385840deb7d7491c2826c41c; 56184 bytes.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/comerr-dev_2\.1-1\.47\.2-3\+b11_armhf\.deb https://snapshot.debian.org/file/c2d2e96220996d61738dcc91ebdf879e9ee2e046
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

# Exact original Socat source archive selected by the MUX source-image recipe.
# Named content preserves extraction type, 579370 bytes, and original SHA256
# adc07a9c2723527cf6568d2fb96559794cf9c254a4bc2edd36f7f3789e9f7625.
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\.orig\.tar\.bz2 https://snapshot.debian.org/file/5a4a6a0e9819a6279e733c6cffd64ac150df93df/socat_1.8.0.2.orig.tar.bz2
rewrite snapshot\.debian\.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1\.8\.0\.2\.orig\.tar\.bz2 https://snapshot.debian.org/archive/debian/20241222T205329Z/pool/main/s/socat/socat_1.8.0.2.orig.tar.bz2

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

# Exact Kerberos inputs from one Orchagent AMD64 source-closure query.
# libkadm5clnt-mit12 failed; the three sibling inputs emitted download warnings.
# Each full official content response matches the accepted lock and retained cache.
# libkadm5clnt-mit12: 37844 bytes; SHA256 166c0b71c91257d725d624ac8bbf345104dce0038451fb6104a0e7eea89278bb.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5clnt\-mit12_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/file/79a0f5873bc04d427cd972471105e1fcf64dd089
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5clnt\-mit12_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5clnt-mit12_1.21.3-5+deb13u1_armhf.deb
# krb5-multidev: 126412 bytes; SHA256 1e9476397e2ee8a749286e60d42fe08abfa0cc4b59703567821fff3d40b66559.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/krb5\-multidev_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/file/20d0dda5ff37855ff607673ee54c7c9608bd9d7b
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/krb5\-multidev_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/krb5-multidev_1.21.3-5+deb13u1_armhf.deb
# libkrb5-3: 284480 bytes; SHA256 988151350dfb736c7bb461ffb24bf136aeae72842838270f44d482b7f70d8167.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-3_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/file/c05801948acf5d6932a3ba59a52413725b1ead42
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-3_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5-3_1.21.3-5+deb13u1_armhf.deb
# libgssapi-krb5-2: 115712 bytes; SHA256 df4ca163a4d682caac2e1fca14d83b6fac3899829856f62fe81f45dc578c7c70.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libgssapi\-krb5\-2_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/file/b52a9657c1c4c021218236cd2c58c2db7b85d55c
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libgssapi\-krb5\-2_1\.21\.3\-5\+deb13u1_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libgssapi-krb5-2_1.21.3-5+deb13u1_armhf.deb

# Exact fatal and warning inputs from PR195 AMD64 archive job127668133187.
# libkrb5-3: 326056 bytes; SHA256
# 47d71d6a7f2e59b9bae5f89602397594805113b95889ad18fa703cd53abafc97.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-3_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/file/53d94056269d541890f7e1bc61b4face03061af6
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5\-3_1\.21\.3\-5\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkrb5-3_1.21.3-5+deb13u1_amd64.deb
# libpcre2-32-0: 268056 bytes; SHA256
# db210bcfa7853c59b1a68863ca7d499122d413d4d368ffcc72c08b26203c3c15.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-32\-0_10\.46\-1\~deb13u1_amd64\.deb https://snapshot.debian.org/file/1d5504c88eac3e34bd38523ed6986c68ab5e205b

# Exact warning inputs from PR195 automatic ARM64 job127668133152.
# tzdata: 264316 bytes; SHA256
# 306013b77311e875620e89f43c447765a6c72b7b398e0baa8e5721bcbbdf66cf.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/t/tzdata/tzdata_2026b\-0\+deb13u1_all\.deb https://snapshot.debian.org/file/47cec4da0609fa1ba1b38b6705c07b8578027cb6
# libk5crypto3 ARM64: 81416 bytes; SHA256
# 2453e8d4fa3b91b868d5bc10e84a15ad9c7bc4e4cb49379cb1f560bdce50db4a.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libk5crypto3_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/file/d747ad6e5b95660c1ca90228b55886aa814a51db
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libk5crypto3_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libk5crypto3_1.21.3-5+deb13u1_arm64.deb

# Exact util-linux ARM64 package selected by an existing automatic native job.
# Verified official content matches accepted lock/cache bytes: 1191312 bytes;
# SHA256 d336901e4c1aedf268837d41a4e1004faa9b7ad4f1e6763f01711b487a4c18ce.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/util\-linux_2\.41\-5_arm64\.deb https://snapshot.debian.org/file/e76be3e653327f16f37ab2c460e45d19b04e79dd

# Exact gzip ARM64 package selected by an existing automatic native job.
# Verified official content matches accepted lock/cache bytes: 135008 bytes;
# SHA256 cd46c9482fd3b96d4ecf282b01185ef9341b0ffcd40816372141f73037ce2c36.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\.13\-1_arm64\.deb https://snapshot.debian.org/file/12136dd88ce463df2f49cdfa4a33666076228318

# Exact gNMI resolver indexes; retain each original dated URL as fallback.
# Accepted same-URL facts and verified full content bytes, not signed Release proof.
# trixie-updates ARMHF: 4408 bytes; SHA256
# c9eb15a380077ab6af13dbc6fb76958e8b96b939cdcb0e999c8e582e619b0fd6.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/dists/trixie\-updates/main/binary\-armhf/Packages\.xz https://snapshot.debian.org/file/ba24766d03291a7cbebe54ae2238ccec3c7861ea
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/dists/trixie\-updates/main/binary\-armhf/Packages\.xz https://snapshot.debian.org/archive/debian/20260727T143429Z/dists/trixie-updates/main/binary-armhf/Packages.xz
# trixie-security AMD64: 226940 bytes; SHA256
# 278bbe9c144f40ddba2af39c9d151349de237542b90e8bec617f63eccba74cba.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/dists/trixie\-security/main/binary\-amd64/Packages\.xz https://snapshot.debian.org/file/8c7898c09763411a9bff9e68bab2c6f08a6b14b4
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/dists/trixie\-security/main/binary\-amd64/Packages\.xz https://snapshot.debian.org/archive/debian-security/20260726T121236Z/dists/trixie-security/main/binary-amd64/Packages.xz

# Exact curl selected by Eventd and MACsec AMD64 dependency analysis.
# The named content response matches the current checked-in lock/cache: 269904
# bytes; SHA256 7bd23df36e65ca32a5bbd81f67ca311b9e0c122212c70d0733f4e45d745789d2.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/curl/curl_8\.14\.1\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/7ba41bdcd2ca506a7d43c424db9cde0662a868ca/curl_8.14.1-2%2Bdeb13u4_amd64.deb

# Exact libaom runtime selected by MUX AMD64 build-tool dependency analysis.
# Verified official content matches the accepted lock and cache: 1871136 bytes;
# SHA256 16321210c4a3e27bf5ad1d56c1f09c9f7958f0c8ec3b8ae894ea1be322c5d055.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/aom/libaom3_3\.12\.1\-1_amd64\.deb https://snapshot.debian.org/file/d8cd6332973da7765c8210e657882014f2c4914d

# Exact libgd3 AMD64 package observed in PR195 native retry analysis.
# Verified official content matches the accepted lock/cache: 126224 bytes;
# SHA256 79ba1a3ceb098236c7fca7d896964eb478b0c3f368d5ec4fd2c1713788033af8.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libg/libgd2/libgd3_2\.3\.3\-13_amd64\.deb https://snapshot.debian.org/file/4c4f480af52f60a63e0b8219b4ec8f2fc31b8bd3

# Exact archive inputs observed in PR196: one fatal and two fallback warnings.
# libaudit-dev arm64; 89868 bytes; SHA256 949459289e2056f526363caf48e741c96ca7706ba3ec359d131160e13c89ef6b.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit\-dev_4\.0\.2\-2\+b2_arm64\.deb https://snapshot.debian.org/file/803c06af36da6b65f1c5641dd9259e28eb2c79d1
# libsmartcols1 amd64; 142692 bytes; SHA256 22ac8bd618c11827e69d710da8b663e07777d3b09969bda0adb968c886e51a97.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util\-linux/libsmartcols1_2\.41\-5_amd64\.deb https://snapshot.debian.org/file/7d770da5d5618391bd3e0d7faa03d1ece944a393
# libssh2-1t64 amd64; 245088 bytes; SHA256 915c4ec450a369d430e0151f9e10e25044ea2f0d6e41901e00a9317e232e5683.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libssh2/libssh2\-1t64_1\.11\.1\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/file/06d870a39f89af810dc74cc6c54947ee57c91374
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libssh2/libssh2\-1t64_1\.11\.1\-1\+deb13u1_amd64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/libs/libssh2/libssh2-1t64_1.11.1-1+deb13u1_amd64.deb

# Exact krb5-multidev ARM64 input observed in Eventd dependency analysis.
# Verified official content matches accepted lock/cache: 126404 bytes;
# SHA256 9f5d65a069269b0960418f9d5a3110158234a1efbe9a9feb3650c57422fe41ef.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/krb5\-multidev_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/file/a7f775b5c78d9d5e769349d980b05b9f13e891d2
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/krb5\-multidev_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/krb5-multidev_1.21.3-5+deb13u1_arm64.deb

# Exact libsodium ARMHF input from the Orchagent AMD64 source-closure query.
# Verified official content matches accepted lock/cache: 138788 bytes;
# SHA256 239ef2d400974ae787c098aadef7c3bc2c81339c36159705010cad6d5e1ed3e3.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1\.0\.18\-1\+deb13u1_armhf\.deb https://snapshot.debian.org/file/60d6945b3aefe2f3abc15790ccddff1ade45376d
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1\.0\.18\-1\+deb13u1_armhf\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1.0.18-1+deb13u1_armhf.deb

# Exact libc6 ARMHF input from the SWSS AMD64 multiarch analysis failure.
# Verified named content preserves 2249772 bytes and locked SHA256
# 8aea0a08cefceb212303a738105ed5befc8fe95a32f465345ab5eec305f21d09.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\.41\-12\+deb13u3_armhf\.deb https://snapshot.debian.org/file/2511d1ea4cdf37ca307cc3336cbc16ff23c323ef/libc6_2.41-12%2Bdeb13u3_armhf.deb

# Exact PCRE16 ARMHF input from PR196 Orchagent owner-source analysis.
# Verified named content preserves 243332 bytes and locked SHA256
# 6f3ed32007e36e2cf1644a0959cfb55a8960dd8837488571900c426a7db4caff.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-16\-0_10\.46\-1\~deb13u1_armhf\.deb https://snapshot.debian.org/file/1f7cf11c8d38946c74712ee3b3ed0e69e222f396/libpcre2-16-0_10.46-1~deb13u1_armhf.deb

# Exact PCRE32 ARMHF input from the AMD64 Orchagent owner source query.
# Official metadata and named content preserve 235052 bytes and locked SHA256
# ab7a0ed7616e15eb569d4df827b6aceb3f6d810959a8970a426614400d92c7e6.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/pcre2/libpcre2\-32\-0_10\.46\-1\~deb13u1_armhf\.deb https://snapshot.debian.org/file/9eb28eee9a02941c85af546838293851576cdfa7/libpcre2-32-0_10.46-1~deb13u1_armhf.deb

# Exact libexpat development ARM64 input from PR195 Python/startup analysis.
# Verified named content preserves 144948 bytes and locked SHA256
# b219482f10e9a0e52474db2a6f239057ea27fea0db0071e3ddb41e8a8a06064c.
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/e/expat/libexpat1\-dev_2\.7\.1\-2_arm64\.deb https://snapshot.debian.org/file/b0fd006c705d3b52a70b6378819d3478dec226f1/libexpat1-dev_2.7.1-2_arm64.deb

# Exact libkadm5srv-mit12 ARM64 input observed in PR151 owner-source analysis.
# Verified named content preserves 53308 bytes and locked SHA256
# 9e25c202e883de84c2c37d8cc10cadc8fd5538cd4227a1b9f4f160ece94756d6.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5srv\-mit12_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/file/5545c41ec7e7c983b9b966ba46680f4b6954f5ad/libkadm5srv-mit12_1.21.3-5%2Bdeb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5srv\-mit12_1\.21\.3\-5\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/k/krb5/libkadm5srv-mit12_1.21.3-5+deb13u1_arm64.deb

# Exact libsodium23 ARM64 input observed in PR151 owner-source analysis.
# Verified named content preserves 120444 bytes and locked SHA256
# d94fc46204b65a965e356ebc19f9f88aeb20a9c777cb9a6569e75f75d0ae5523.
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1\.0\.18\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/file/283449abed3611000399635e3d5dc333b29c4c08/libsodium23_1.0.18-1%2Bdeb13u1_arm64.deb
rewrite snapshot\.debian\.org/archive/debian\-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1\.0\.18\-1\+deb13u1_arm64\.deb https://snapshot.debian.org/archive/debian-security/20260726T121236Z/pool/updates/main/libs/libsodium/libsodium23_1.0.18-1+deb13u1_arm64.deb

# Timestamped Debian binary packages: same pool path on the official CDN first,
# then the exact snapshot URL. Bazel still checks the locked package SHA256.
# These generic rules exclude indexes, source archives, and debian-security.
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2

# Cloudflare snapshot package inputs retain CDN, standard snapshot, then original.
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://deb.debian.org/debian/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot.debian.org/archive/debian/$1/$2
rewrite snapshot-cloudflare\.debian\.org/archive/debian/([0-9]{8}T[0-9]{6}Z)/(pool/(?:[A-Za-z0-9][A-Za-z0-9_+.-]*/)+[A-Za-z0-9][A-Za-z0-9_+.%:~-]*\.deb) https://snapshot-cloudflare.debian.org/archive/debian/$1/$2

# P4RT owner4a5 pins this XZ source through rules_boost 5160325d.
# The retained official release matches 2889306 bytes and SHA256
# aeba3e03bf8140ddedf62a0a367158340520f6b384f75ca6045ccc6c0d43fd5c.
rewrite src\.fedoraproject\.org/lookaside/extras/xz/xz\-5\.4\.6\.tar\.gz/sha512/b08a61d8d478d3b4675cb1ddacdbbd98dc6941a55bcdd81a28679e54e9367d3a595fa123ac97874a17da571c1b712e2a3e901c2737099a9d268616a1ba3de497/xz\-5\.4\.6\.tar\.gz https://github.com/tukaani-project/xz/releases/download/v5.4.6/xz-5.4.6.tar.gz
rewrite src\.fedoraproject\.org/lookaside/extras/xz/xz\-5\.4\.6\.tar\.gz/sha512/b08a61d8d478d3b4675cb1ddacdbbd98dc6941a55bcdd81a28679e54e9367d3a595fa123ac97874a17da571c1b712e2a3e901c2737099a9d268616a1ba3de497/xz\-5\.4\.6\.tar\.gz https://src.fedoraproject.org/lookaside/extras/xz/xz-5.4.6.tar.gz/sha512/b08a61d8d478d3b4675cb1ddacdbbd98dc6941a55bcdd81a28679e54e9367d3a595fa123ac97874a17da571c1b712e2a3e901c2737099a9d268616a1ba3de497/xz-5.4.6.tar.gz
"""

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("source_image_worker", ROOT / "tools/bazel/ci/source_image_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class ImageWorkerTest(unittest.TestCase):
    def network_result(self, uid):
        return {"uid": uid, "proxy_names_present": {name: False for name in
                ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")},
                "urllib_proxy_schemes": [], "http_status": 200}

    def test_execution_network_child_redacts_values_and_preserves_normal_tls(self):
        from unittest.mock import MagicMock
        import urllib.error
        secret = "https://private-user:private-password@private-proxy.example/?token=private-token"
        response = MagicMock()
        response.__enter__.return_value.status = 200
        for failure, expected in ((None, {"http_status": 200}),
                                  (urllib.error.URLError(secret), {"error_class": "URLError"}),
                                  (urllib.error.HTTPError(secret, 503, secret, {}, None),
                                   {"error_class": "HTTPError", "http_status": 503})):
            with self.subTest(failure=expected), patch.dict(os.environ, {"https_proxy": secret}, clear=True), \
                    patch.object(os, 'getuid', return_value=1000), \
                    patch.object(worker.urllib.request, 'getproxies', return_value={'https': secret, 'private-scheme': secret}), \
                    patch.object(worker.urllib.request, 'urlopen', return_value=response, side_effect=failure) as opened, \
                    patch('sys.stdout', new_callable=io.StringIO) as stream:
                exec(compile(worker.EXECUTION_NETWORK_PROBE, '<fixed-network-probe>', 'exec'), {})
            value = json.loads(stream.getvalue())
            self.assertEqual(value['uid'], 1000)
            self.assertTrue(value['proxy_names_present']['https_proxy'])
            self.assertFalse(value['proxy_names_present']['HTTPS_PROXY'])
            self.assertEqual(value['urllib_proxy_schemes'], ['https'])
            self.assertEqual({k: value[k] for k in ('http_status', 'error_class') if k in value}, expected)
            self.assertNotIn('private-', stream.getvalue())
            opened.assert_called_once_with('https://deb.debian.org/debian/dists/trixie/InRelease', timeout=10)
            worker.safe_json(value)

    def test_execution_network_uses_actual_worker_and_sudo_contexts_and_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            with patch.object(worker, 'output', side_effect=[json.dumps(self.network_result(1000)),
                                                            json.dumps(self.network_result(0))]) as output:
                with patch.object(worker, 'prepare_apt_transport') as apt:
                    worker.prepare_execution_network(artifacts)
                apt.assert_called_once_with(artifacts)
            self.assertEqual([call.args[0] for call in output.call_args_list], [
                [sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE],
                ['sudo', '-n', sys.executable, '-I', '-c', worker.EXECUTION_NETWORK_PROBE]])
            for call in output.call_args_list:
                self.assertEqual(call.kwargs, {'timeout': 20, 'stderr': subprocess.DEVNULL})
            path = artifacts / 'worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': self.network_result(1000), 'sudo': self.network_result(0)})
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertEqual(path.read_bytes(), (artifacts / 'public' / path.name).read_bytes())

    def apt_probe(self):
        namespace = {'__name__': 'fixed_apt_diagnostic_test'}
        exec(compile(worker.APT_TRANSPORT_PROBE, '<fixed-apt-probe>', 'exec'), namespace)
        return namespace

    def test_apt_configuration_redacts_fixed_values_and_rejects_malformed_output(self):
        import shlex
        probe = self.apt_probe()
        secret = 'https://private-user:private-password@private-host/\nprivate-token'
        raw = 'K0=' + shlex.quote(secret) + "\nK8='true'\nK9='_apt'\nK10='false'\n"
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=raw)) as query:
            value = probe['configuration']()
        self.assertNotIn('private-', json.dumps(value))
        self.assertEqual(value['proxy_entries_present'], [True] + [False] * 7)
        self.assertFalse(value['srv_default'])
        args = query.call_args.args[0]
        self.assertEqual(args[:2], ['apt-get', 'shell'])
        self.assertEqual(query.call_args.kwargs, dict(executable='/usr/bin/apt-config', check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5))
        self.assertEqual(len(args[2:]), 66)
        for raw in ("K0='x' K0='y'", "UNKNOWN='private-secret'", "K8='private-secret'", "K0='unclosed", 'x' * 65537):
            with self.subTest(raw=raw[:30]), patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=raw)):
                with self.assertRaises(ValueError):
                    probe['configuration']()
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout="K11='private-secret'")):
            value = probe['configuration']()
        self.assertTrue(value['method_overrides_present'])
        self.assertTrue(value['srv_default'])
        self.assertTrue(value['srv_enabled'])

    def test_apt_helper_order_private_destination_and_inherited_file_limit(self):
        probe = self.apt_probe()
        config = dict(proxy_entries_present=[False] * 8, srv_enabled=True, srv_default=True,
                      sandbox_kind='_apt', no_drop_privs=False, method_overrides_present=False, apt_config_present=False)
        events = []
        def download(directory, disabled):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            events.append(('download', disabled))
            if not disabled:
                raise RuntimeError('private-secret')
            return {'status': 'exit_failure', 'exit_code': 100}
        with patch.dict(probe, {'configuration': lambda: config, 'download': download}), \
                patch.object(os, 'getuid', return_value=0), \
                patch.object(probe['pwd'], 'getpwnam', return_value=SimpleNamespace(pw_uid=42, pw_gid=42)) as account, \
                patch.object(probe['resource'], 'setrlimit', side_effect=lambda *args: events.append(('limit', args))), \
                patch.object(os, 'umask') as mask, patch.object(os, 'chown') as owner:
            result = probe['main']()
        account.assert_called_once_with('_apt')
        owner.assert_called_once()
        self.assertEqual(owner.call_args.args[1:], (42, 42))
        mask.assert_called_once_with(0o077)
        self.assertEqual(events, [('limit', (probe['resource'].RLIMIT_FSIZE, (1048576, 1048576))),
                                  ('download', False), ('download', True)])
        self.assertEqual(result['attempts'], [{'status': 'error', 'error_class': 'RuntimeError'},
                                             {'status': 'exit_failure', 'exit_code': 100}])
        self.assertNotIn('private-', json.dumps(result))
        for changed in ({'method_overrides_present': True}, {'sandbox_kind': 'other'}):
            with patch.dict(probe, {'configuration': lambda: {**config, **changed}}), \
                    patch.object(os, 'getuid', return_value=0), patch.dict(probe, {'download': unittest.mock.Mock()}):
                result = probe['main']()
                probe['download'].assert_not_called()
            self.assertEqual(result['status'], 'unsupported_configuration')

    def test_apt_helper_commands_size_checks_and_no_raw_output(self):
        from unittest.mock import Mock
        probe = self.apt_probe()
        for disabled in (False, True):
            for payload, expected in ((b'fixed InRelease bytes', 'downloaded'), (b'', 'size_or_type_rejected'),
                                      (b'x' * (1048576 + 1), 'size_or_type_rejected')):
                with tempfile.TemporaryDirectory() as directory:
                    process = Mock(pid=123456)
                    process.wait.return_value = 0
                    path = Path(directory) / ('srv-disabled' if disabled else 'baseline')
                    def start(args, **kwargs):
                        path.write_bytes(payload)
                        return process
                    with patch.object(subprocess, 'Popen', side_effect=start) as popen, patch.object(os, 'killpg'), \
                            patch.object(os, 'waitid', return_value=SimpleNamespace(si_code=os.CLD_EXITED, si_status=0)), \
                            patch.object(probe['time'], 'sleep'):
                        result = probe['download'](directory, disabled)
                    args = ['apt-get', '-o', 'Acquire::Retries=0', '-o', 'Acquire::http::Timeout=10',
                            '-o', 'Acquire::https::Timeout=10']
                    if disabled:
                        args += ['-o', 'Acquire::EnableSrvRecords=false']
                    args += ['download-file', 'https://deb.debian.org/debian/dists/trixie/InRelease', str(path)]
                    popen.assert_called_once_with(args, executable='/usr/lib/apt/apt-helper', start_new_session=True,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.assertEqual(result['status'], expected)
                    if expected == 'downloaded':
                        self.assertEqual(result['bytes'], len(payload))
                        self.assertEqual(result['sha256'], hashlib.sha256(payload).hexdigest())
                    else:
                        self.assertNotIn('sha256', result)

    def test_apt_helper_timeout_terminates_only_its_own_process_group(self):
        from unittest.mock import Mock, call
        probe = self.apt_probe()
        process = Mock(pid=123456)
        process.wait.return_value = -9
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'killpg') as kill, patch.object(os, 'waitid', return_value=None) as observe, \
                patch.object(probe['time'], 'monotonic', side_effect=[0, 0, 16]), patch.object(probe['time'], 'sleep'):
            result = probe['download'](directory, False)
        self.assertEqual(result, {'status': 'timeout'})
        self.assertEqual(kill.call_args_list, [call(123456, probe['signal'].SIGTERM), call(123456, probe['signal'].SIGKILL)])
        observe.assert_called_once_with(os.P_PID, 123456, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        process.wait.assert_called_once_with(timeout=2)

    def test_apt_helper_exited_leader_is_not_reaped_until_descendants_are_killed(self):
        from unittest.mock import Mock
        probe = self.apt_probe()
        events = []
        process = Mock(pid=123456)
        process.wait.side_effect = lambda **kwargs: events.append(('reap', kwargs)) or 100
        def observe(*args):
            events.append(('observe_unreaped', args))
            return SimpleNamespace(si_code=os.CLD_EXITED, si_status=100)
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'waitid', side_effect=observe), \
                patch.object(os, 'killpg', side_effect=lambda *args: events.append(('signal', args))), \
                patch.object(probe['time'], 'sleep', side_effect=lambda delay: events.append(('grace', delay))):
            result = probe['download'](directory, False)
        self.assertEqual(result, {'status': 'exit_failure', 'exit_code': 100})
        self.assertEqual(events, [('observe_unreaped', (os.P_PID, 123456, os.WEXITED | os.WNOHANG | os.WNOWAIT)),
                                 ('signal', (123456, probe['signal'].SIGTERM)), ('grace', 2),
                                 ('signal', (123456, probe['signal'].SIGKILL)), ('reap', {'timeout': 2})])
        with tempfile.TemporaryDirectory() as directory, patch.object(subprocess, 'Popen', return_value=process), \
                patch.object(os, 'waitid', side_effect=ChildProcessError('private-secret')), patch.object(os, 'killpg') as kill:
            with self.assertRaises(ChildProcessError):
                probe['download'](directory, False)
        kill.assert_not_called()

    def test_apt_transport_receipt_rejects_secrets_and_remains_publishable(self):
        result = {'scope': 'transport_only_no_signature_verification', 'status': 'completed',
                  'config': dict(proxy_entries_present=[False] * 8, srv_enabled=True, srv_default=True,
                                 sandbox_kind='_apt', no_drop_privs=False, method_overrides_present=False,
                                 apt_config_present=False),
                  'attempts': [{'status': 'exit_failure', 'exit_code': 100},
                               {'status': 'downloaded', 'exit_code': 0, 'bytes': 14, 'sha256': 'a' * 64}]}
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            with patch.object(worker, 'output', return_value=json.dumps(result)) as output:
                worker.prepare_apt_transport(artifacts)
            output.assert_called_once_with(['sudo', '-n', sys.executable, '-I', '-c', worker.APT_TRANSPORT_PROBE],
                                           timeout=50, stderr=subprocess.DEVNULL)
            path = artifacts / 'worker-apt-transport.json'
            self.assertEqual(json.loads(path.read_text()), result)
            worker.publish(SimpleNamespace(artifacts=artifacts))
            self.assertEqual(path.read_bytes(), (artifacts / 'public' / path.name).read_bytes())
            invalid = [dict(result, extra='private-password'), dict(result, error_class='private-password://host'),
                       {'scope': result['scope'], 'status': 'completed'},
                       {**result, 'config': {**result['config'], 'sandbox_kind': 'private-account'}},
                       {**result, 'attempts': [{'status': 'error', 'stderr': 'private-token'}] * 2},
                       {**result, 'attempts': [{'status': 'downloaded', 'exit_code': 0, 'bytes': 1048577, 'sha256': 'a' * 64}] * 2}]
            for value in invalid:
                with patch.object(worker, 'output', return_value=json.dumps(value)):
                    worker.prepare_apt_transport(artifacts)
                self.assertEqual(json.loads(path.read_text()), {'status': 'error', 'error_class': 'ValueError'})
                self.assertNotIn('private-', path.read_text())

    def test_execution_network_failure_and_unexpected_identity_are_nonfatal_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            failures = [subprocess.CalledProcessError(1, ['private-command'], output='private-auth', stderr='private-password'),
                        subprocess.TimeoutExpired(['private-command'], 20, output='private-token')]
            with patch.object(worker, 'output', side_effect=failures):
                worker.prepare_execution_network(artifacts)
            path = artifacts / 'worker-execution-network.json'
            self.assertEqual(json.loads(path.read_text()), {'worker': {'error_class': 'CalledProcessError'},
                                                          'sudo': {'error_class': 'TimeoutExpired'}})
            self.assertNotIn('private-', path.read_text())
            for value in (self.network_result(0), {**self.network_result(1000), 'proxy_names_present': {'https_proxy': 'private-password'}},
                          {**self.network_result(1000), 'error_class': 'private-auth://url'},
                          {**self.network_result(1000), 'extra': 'private-token'}):
                with patch.object(worker, 'output', side_effect=[json.dumps(value), json.dumps(self.network_result(0))]):
                    worker.prepare_execution_network(artifacts)
                self.assertEqual(json.loads(path.read_text())['worker'], {'error_class': 'ValueError'})
                self.assertNotIn('private-', path.read_text())

    def test_execution_network_only_after_apt_failure_and_preserves_original(self):
        update = ['sudo', '-n', 'apt-get', 'update']
        install = ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get',
                   'install', '-y', '--no-install-recommends', 'libarchive-tools']
        for failure_at in (0, 1):
            for diagnostic_failure in (False, True):
                with self.subTest(failure_at=failure_at, diagnostic_failure=diagnostic_failure), \
                        tempfile.TemporaryDirectory() as directory:
                    artifacts = Path(directory)
                    commands = []
                    original = subprocess.CalledProcessError(100, [update, install][failure_at])
                    def apt(command):
                        commands.append(command)
                        if len(commands) == failure_at + 1:
                            raise original
                    def diagnose(path):
                        self.assertEqual(path, artifacts)
                        commands.append('diagnostic')
                        if diagnostic_failure:
                            raise RuntimeError('private-secret-value')
                    with patch.object(worker.shutil, 'which', side_effect=lambda tool: None if tool == 'bsdtar' else '/usr/bin/' + tool), \
                            patch.object(worker, 'run', side_effect=apt), \
                            patch.object(worker, 'prepare_execution_network', side_effect=diagnose), \
                            patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                            self.assertRaises(subprocess.CalledProcessError) as raised:
                        worker.prepare_validation_tools(artifacts)
                    self.assertIs(raised.exception, original)
                    self.assertEqual(commands, [update, install][:failure_at + 1] + ['diagnostic'])
                    self.assertNotIn('private-', stderr.getvalue())
                    self.assertFalse((artifacts / 'worker-execution-tools.json').exists())

    def test_execution_network_is_skipped_when_apt_succeeds_or_tools_exist(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                commands = []
                def available(tool):
                    return None if missing and tool == 'bsdtar' and not commands else '/usr/bin/' + tool
                with patch.object(worker.shutil, 'which', side_effect=available), \
                        patch.object(worker, 'run', side_effect=lambda command: commands.append(command)), \
                        patch.object(worker, 'output', return_value='versions'), \
                        patch.object(worker, 'prepare_execution_network') as diagnostic, \
                        patch.object(worker.urllib.request, 'urlopen') as opened:
                    worker.prepare_validation_tools(Path(directory))
                diagnostic.assert_not_called()
                opened.assert_not_called()
                self.assertEqual(len(commands), 2 if missing else 0)

    def test_teamd_kernel_reads_preloaded_directories_and_retains_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules, artifacts = root / 'modules', root / 'artifacts'
            modules.mkdir(); artifacts.mkdir()
            with patch.object(worker, 'run') as run, patch.object(worker, 'output') as output:
                for name in ('team', 'team_mode_loadbalance'):
                    with self.assertRaisesRegex(ValueError, 'preloaded kernel modules'):
                        worker.prepare_teamd_kernel(artifacts, modules)
                    receipt = json.loads((artifacts / 'worker-teamd-kernel.json').read_text())
                    self.assertEqual(receipt['status'], 'failed')
                    self.assertIn(name, receipt['missing_modules'])
                    (modules / name).write_text('regular file is not a loaded module')
                    with self.assertRaises(ValueError):
                        worker.prepare_teamd_kernel(artifacts, modules)
                    (modules / name).unlink(); (modules / name).mkdir()
                worker.prepare_teamd_kernel(artifacts, modules)
                receipt = json.loads((artifacts / 'worker-teamd-kernel.json').read_text())
                self.assertEqual(receipt, {'status': 'passed', 'read_only': True,
                    'required_modules': ['team', 'team_mode_loadbalance'], 'missing_modules': [],
                    'preloaded_modules': ['team', 'team_mode_loadbalance']})
                run.assert_not_called(); output.assert_not_called()

    def test_teamd_missing_modules_precede_all_direct_preparation_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            gate = worker.prepare_teamd_kernel
            with patch.object(worker, 'prepare_teamd_kernel',
                              side_effect=lambda out: gate(out, artifacts / 'missing')) as checked, \
                    patch.object(worker, 'measure') as measure, \
                    patch.object(worker, 'prepare_validation_tools') as tools, \
                    patch.object(worker, 'prepare_source_downloads') as downloads, \
                    patch.object(worker, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'preloaded kernel modules'):
                    worker.prepare(SimpleNamespace(image='teamd', workspace=Path('/source'), artifacts=artifacts))
                checked.assert_called_once_with(artifacts)
                for command in (measure, tools, downloads, run): command.assert_not_called()
            worker.publish(SimpleNamespace(artifacts=artifacts, image='teamd'))
            retained = json.loads((artifacts / 'public/worker-teamd-kernel.json').read_text())
            self.assertEqual(retained['status'], 'failed')
            self.assertEqual(retained['missing_modules'], ['team', 'team_mode_loadbalance'])

    def test_teamd_workflow_checks_modules_before_checkout_download_or_setup(self):
        workflow = (ROOT / '.github/workflows/bazel-oci.yml').read_text()
        gate = workflow.index('- name: Verify Teamd kernel prerequisites without host changes')
        end = workflow.index('- name: Checkout the exact reviewed source', gate)
        step = workflow[gate:end]
        self.assertIn("if: inputs.source_image == 'teamd'", step)
        self.assertIn('source_image_worker.py" kernel-preflight', step)
        for name in ('Checkout the exact reviewed source', 'Reuse the existing source-download artifact',
                     'Download the verified source archives', 'Prepare private Docker and persistent worker caches'):
            self.assertLess(gate, workflow.index('- name: ' + name))
        self.assertIn("inputs.source_image == 'teamd' && steps.teamd_kernel.outcome != 'skipped'", workflow)

    def test_publication_retains_the_actual_aggregate_loaded_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            receipt = {'status': 'passed', 'revision': 'a' * 40, 'architecture': 'amd64',
                       'images': {'docker-teamd': {'status': 'passed'},
                                  'docker-teamd-dbg': {'status': 'passed'}}}
            source = artifacts / 'loaded-validation.json'
            source.write_text(json.dumps(receipt, indent=2) + '\n')
            worker.publish(SimpleNamespace(artifacts=artifacts, image='teamd'))
            self.assertEqual((artifacts / 'public/loaded-validation.json').read_bytes(), source.read_bytes())

    def test_teamd_fixed_driver_reuses_compatibility_wrapper_and_four_phases(self):
        workspace, artifacts = Path('/source'), Path('/evidence')
        self.assertEqual(worker.WRAPPERS['teamd'], 'libteam_draft_bazel.sh')
        for phase in ('build', 'validate', 'incremental'):
            expected = [sys.executable, '-B', '/source/tools/bazel/ci/teamd_image.py', phase,
                        '--artifacts', str(artifacts)]
            if phase != 'validate': expected += ['--bazel', '/source/tools/bazel/ci/libteam_draft_bazel.sh']
            self.assertEqual(worker.phase_command('teamd', phase, workspace, artifacts), expected)
        self.assertEqual(worker.phase_command('teamd', 'loaded', workspace, artifacts),
                         [sys.executable, '-B', '/source/tools/bazel/ci/teamd_loaded.py', '--artifacts', str(artifacts)])

    def native_job(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())["jobs"]["source_image"]

    def native_paths(self, root):
        workspace, temporary = root / "workspace", root / "temporary"
        workspace.mkdir(); temporary.mkdir()
        environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temporary),
                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_NAME": "sonic-slave-lgh114-1",
                       "EXPECTED_WORKER": "sonic-slave-lgh114-1", "SELECTED_IMAGE": "dash-engine",
                       "SOURCE_REVISION": "a" * 40}
        for kind in ("CONTROL", "SOURCE", "SCRATCH"):
            environment["SOURCE_WORKER_" + kind] = "source-image-" + kind.lower() + "-123-2-sonic-slave-lgh114-1"
        return workspace, temporary, environment

    def test_native_workflow_preserves_old_work_and_binds_every_driver_invocation(self):
        job = self.native_job()
        steps = {step["name"]: step for step in job["steps"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, temporary, environment = self.native_paths(root)
            old = [workspace / "control/keep", workspace / "source/target/output",
                   workspace / "source/.bazelrc.user", temporary / "source-image-artifacts/receipt.json",
                   temporary / "source-image-inputs/input.tar.gz"]
            for path in old:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("retained")
            subprocess.run(["bash", "-e", "-c", steps["Require fresh native worker paths"]["run"]],
                           env=environment, cwd=workspace, check=True, capture_output=True)
            control = workspace / environment["SOURCE_WORKER_CONTROL"]
            helper = control / "tools/bazel/ci/source_image_worker.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("import json,os,sys\nfrom pathlib import Path\n"
                              "with Path(os.environ['TRACE']).open('a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n")
            trace = root / "trace.jsonl"; environment["TRACE"] = str(trace)
            for step in job["steps"][1:]:
                if step.get("if") in ("inputs.source_image == 'teamd'", "inputs.source_image == 'p4rt'"):
                    continue
                if "run" in step:
                    subprocess.run(["bash", "-e", "-c", step["run"]], env=environment,
                                   cwd=workspace, check=True, capture_output=True)
            commands = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([cmd[0] for cmd in commands],
                             ["selection", "verify-input-run", "prepare", "build", "validate", "incremental", "loaded", "publish", "stop"])
            for cmd in commands:
                self.assertEqual(cmd[1:], ["--image", "dash-engine", "--revision", "a" * 40,
                    "--workspace", str(workspace / environment["SOURCE_WORKER_SOURCE"]),
                    "--artifacts", str(temporary / environment["SOURCE_WORKER_SCRATCH"] / "artifacts")])
            for path in old:
                self.assertEqual(path.read_text(), "retained")
        for name, variable in (("Checkout reviewed dispatcher", "SOURCE_WORKER_CONTROL"),
                               ("Checkout the exact reviewed source", "SOURCE_WORKER_SOURCE")):
            self.assertEqual(steps[name]["with"]["path"], "${{ env." + variable + " }}")
            self.assertIs(steps[name]["with"]["clean"], False)
            self.assertIs(steps[name]["with"]["persist-credentials"], False)
        for name in ("Reuse the existing source-download artifact", "Download the verified source archives"):
            self.assertEqual(steps[name]["with"]["path"], "${{ runner.temp }}/${{ env.SOURCE_WORKER_SCRATCH }}/source-image-inputs")
        self.assertEqual(steps["Upload source images and acceptance receipts"]["with"]["path"],
                         "${{ runner.temp }}/${{ env.SOURCE_WORKER_SCRATCH }}/artifacts/public/")

    def test_native_path_gate_refuses_collisions_and_wrong_identities_without_deleting(self):
        guard = self.native_job()["steps"][0]["run"]
        cases = [(kind, linked) for kind in ("CONTROL", "SOURCE", "SCRATCH") for linked in (False, True)]
        cases += [("worker", False), ("attempt", False), ("traversal", False)]
        for kind, linked in cases:
            with self.subTest(kind=kind, linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace, temporary, environment = self.native_paths(root)
                keep = root / "keep"; keep.write_text("retained")
                collision = None
                if kind in ("CONTROL", "SOURCE", "SCRATCH"):
                    collision = (temporary if kind == "SCRATCH" else workspace) / environment["SOURCE_WORKER_" + kind]
                    if linked:
                        collision.symlink_to(root / "missing")
                    else:
                        collision.mkdir(); (collision / "keep").write_text("retained")
                elif kind == "worker": environment["RUNNER_NAME"] = "sonic-slave-lgh114-2"
                elif kind == "attempt": environment["GITHUB_RUN_ATTEMPT"] = "../2"
                else: environment["SOURCE_WORKER_SOURCE"] = "../" + environment["SOURCE_WORKER_SOURCE"]
                result = subprocess.run(["bash", "-e", "-c", guard], env=environment, cwd=workspace, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(keep.read_text(), "retained")
                if collision is not None:
                    if linked: self.assertTrue(collision.is_symlink())
                    else: self.assertEqual((collision / "keep").read_text(), "retained")

    def test_worker_temporary_directory_is_bound_to_run_attempt_and_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, temporary, environment = self.native_paths(root)
            with patch.dict(os.environ, environment):
                expected = temporary / environment["SOURCE_WORKER_SCRATCH"]
                self.assertEqual(worker.temporary_directory(), expected)
                self.assertFalse(expected.exists())
                for changed in ("../scratch", "source-image-scratch-123-1-sonic-slave-lgh114-1",
                                "source-image-scratch-123-2-sonic-slave-lgh114-2"):
                    with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": changed}), self.assertRaises(ValueError):
                        worker.temporary_directory()
                expected.symlink_to(root / "missing")
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    worker.temporary_directory()
                with patch.dict(os.environ, {"SOURCE_WORKER_SCRATCH": ""}):
                    self.assertEqual(worker.temporary_directory(), temporary)

    def test_optional_catalog_requires_an_immutable_source_pair(self):
        for catalog in ("", "b" * 40):
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": catalog}):
                worker.selection("dash-engine", "a" * 40)
                with self.assertRaises(ValueError): worker.selection("dash-engine", "main")
        for catalog in ("main", "B" * 40, "b" * 39, "b" * 40 + ";id"):
            with self.subTest(catalog=catalog), patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": catalog}), \
                    self.assertRaises(ValueError):
                worker.selection("dash-engine", "a" * 40)

    def test_explicit_catalog_uses_review_entrypoint_without_changing_blank_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "tools/bazel/ci").mkdir(parents=True)
            path = root / ".github/workflows/bazel-oci.yml"
            path.write_text("env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-engine", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-engine", root, "b" * 40), "b" * 40)
            for catalog in ("main", "B" * 40, "b" * 39):
                with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                    worker.catalog_for("dash-engine", root, catalog)
            path.write_text(path.read_text().replace("review_bazel.sh", "libteam_draft_bazel.sh"))
            (root / "tools/bazel/ci/libteam_draft_bazel.sh").write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-ha", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-ha", root, "a" * 40), "a" * 40)
            with self.assertRaisesRegex(ValueError, "cannot override"):
                worker.catalog_for("dash-ha", root, "b" * 40)

    def test_changed_explicit_catalog_is_rejected_before_a_phase_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(image="dash-engine", revision="a" * 40, workspace=Path("/source"),
                                   artifacts=Path(directory), phase="build")
            state = {"image": args.image, "revision": args.revision, "workspace": str(args.workspace),
                     "environment": {"SONIC_BAZEL_REGISTRY_REVISION": "b" * 40}}
            (args.artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": "b" * 40}):
                self.assertEqual(worker.state_for(args), state)
            with patch.dict(os.environ, {"SOURCE_REGISTRY_REVISION": "c" * 40}), \
                    patch.object(worker.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "Prepared catalog"):
                worker.execute_phase(args)
            run.assert_not_called()

    def test_syncd_tap_requires_exact_device_and_private_namespace_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            device = SimpleNamespace(st_mode=stat.S_IFCHR | 0o666, st_rdev=os.makedev(10, 200))
            proof = {"private_network_namespace": True, "tap_created_and_closed": True}
            with patch.object(worker.Path, 'lstat', return_value=device), \
                    patch.object(worker.Path, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                    patch.object(worker, 'output', return_value=json.dumps(proof)) as output:
                worker.prepare_tap_fixture(artifacts)
                command = output.call_args.args[0]
                self.assertEqual(command[:5], ['sudo', '-n', 'unshare', '--net', '--'])
                self.assertEqual(command[5:8], [sys.executable, '-I', '-c'])
                self.assertEqual(command[-1], '42')
                self.assertEqual(output.call_args.kwargs, {'timeout': 30})
                self.assertEqual(json.loads((artifacts / 'worker-tap.json').read_text())['status'], 'passed')
                for mode, number in ((stat.S_IFREG, os.makedev(10, 200)),
                                     (stat.S_IFCHR, os.makedev(10, 201)),
                                     (stat.S_IFLNK, os.makedev(10, 200))):
                    device.st_mode, device.st_rdev = mode, number
                    with self.assertRaisesRegex(ValueError, 'character device'):
                        worker.prepare_tap_fixture(artifacts)
                device.st_mode, device.st_rdev = stat.S_IFCHR, os.makedev(10, 200)
                output.return_value = json.dumps({**proof, 'private_network_namespace': False})
                with self.assertRaisesRegex(ValueError, 'Incomplete private TAP proof'):
                    worker.prepare_tap_fixture(artifacts)
                output.side_effect = subprocess.CalledProcessError(1, command)
                with self.assertRaises(subprocess.CalledProcessError):
                    worker.prepare_tap_fixture(artifacts)

    def test_syncd_tap_child_rejects_shared_namespace_and_closes_descriptor(self):
        import fcntl
        import struct
        from unittest.mock import mock_open
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(worker.Path, 'lstat', return_value=SimpleNamespace(
                    st_mode=stat.S_IFCHR, st_rdev=os.makedev(10, 200))), \
                patch.object(worker.Path, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                patch.object(worker, 'output', return_value='{"private_network_namespace":true,"tap_created_and_closed":true}') as output:
            worker.prepare_tap_fixture(Path(directory))
            script = output.call_args.args[0][-2]
        with patch.object(sys, 'argv', ['tap-probe', '42']), \
                patch.object(os, 'stat', return_value=SimpleNamespace(st_ino=42)), \
                patch('builtins.open') as opened, self.assertRaisesRegex(SystemExit, 'private network namespace'):
            exec(compile(script, '<tap-probe>', 'exec'), {})
            opened.assert_not_called()
        opened = mock_open()
        opened.return_value.fileno.return_value = 7
        with patch.object(sys, 'argv', ['tap-probe', '42']), \
                patch.object(os, 'stat', return_value=SimpleNamespace(st_ino=99)), \
                patch('builtins.open', opened), patch.object(fcntl, 'ioctl', return_value=b'sonic-probe0\0' + b'\0' * 6) as ioctl, \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            exec(compile(script, '<tap-probe>', 'exec'), {})
            ioctl.assert_called_once_with(7, 0x400454ca, struct.pack('16sH', b'sonic-probe%d', 0x0002 | 0x1000))
            opened.return_value.__exit__.assert_called_once()
            self.assertTrue(json.loads(stdout.getvalue())['tap_created_and_closed'])

    def test_syncd_tap_gate_precedes_downloads_and_bazel_setup(self):
        source = inspect.getsource(worker.prepare)
        self.assertIn('if args.image == "syncd":\n        prepare_tap_fixture(artifacts)', source)
        self.assertLess(source.index('prepare_tap_fixture(artifacts)'), source.index('prepare_source_downloads('))
        self.assertLess(source.index('prepare_tap_fixture(artifacts)'), source.index('launcher_text('))

    def test_validation_bootstrap_installs_only_missing_execution_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            def available(tool):
                return None if tool == 'bsdtar' and not commands else '/usr/bin/' + tool
            commands = []
            with patch.object(worker.shutil, 'which', side_effect=available), \
                    patch.object(worker, 'run', side_effect=lambda command: commands.append(command)), \
                    patch.object(worker, 'output', return_value='libarchive-tools\t3.7.4'):
                worker.prepare_validation_tools(artifacts)
            self.assertEqual(commands, [
                ['sudo', '-n', 'apt-get', 'update'],
                ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
                 '--no-install-recommends', 'libarchive-tools']])
            receipt = json.loads((artifacts / 'worker-execution-tools.json').read_text())
            self.assertEqual(receipt['packages_installed'], ['libarchive-tools'])
            with patch.object(worker.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(worker, 'run') as run, patch.object(worker, 'output', return_value='versions'):
                worker.prepare_validation_tools(artifacts)
                run.assert_not_called()
            with patch.object(worker.shutil, 'which', return_value=None), \
                    patch.object(worker, 'run'), self.assertRaisesRegex(ValueError, 'Missing image validation tool'):
                worker.prepare_validation_tools(artifacts)

    def test_gawk_mirror_changes_only_three_exact_source_urls(self):
        # Bind the actual immutable base while preserving the existing suffix rules.
        actual = SHARED_CONFIG_FIXTURE.encode()
        self.assertEqual(worker.MIRROR_URL,
                         "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/8ba36072602dc98220d20834fdb8eb189c880afd/ci/source_mirrors.cfg")
        self.assertEqual(hashlib.sha256(actual).hexdigest(), worker.MIRROR_SHA)
        self.assertTrue(worker.mirror_configuration(actual).startswith(actual))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            worker.mirror_configuration(actual + b'changed')
        original = b"verified existing mirror fixture\n"
        with patch.object(worker, 'MIRROR_SHA', hashlib.sha256(original).hexdigest()):
            config = worker.mirror_configuration(original).decode()
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                worker.mirror_configuration(original + b'changed')
        rewrites = [line.split() for line in config.splitlines()
                    if line.startswith('rewrite ') and 'gawk' in line]
        self.assertEqual(len(rewrites), 3)
        for host, (_, expression, destination) in zip(
                ('mirrors.kernel.org', 'ftp.gnu.org', 'ftpmirror.gnu.org'), rewrites):
            self.assertTrue(re.fullmatch(expression, host + '/gnu/gawk/gawk-5.3.2.tar.xz'))
            self.assertFalse(re.fullmatch(expression, host + '/gnu/gawk/gawk-5.3.3.tar.xz'))
            self.assertEqual(destination, 'https://mirror.bazel.build/ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz')

    def test_snapshot_fallbacks_keep_original_and_only_exact_observed_urls(self):
        original = b'prior verified mirror rules\n'
        with patch.object(worker, 'MIRROR_SHA', hashlib.sha256(original).hexdigest()):
            config = worker.mirror_configuration(original).decode()
        rules = [line.split()[1:] for line in config.splitlines()
                 if line.startswith('rewrite ') and 'snapshot' in line]
        self.assertEqual(len(rules), 8)
        self.assertEqual(len(worker.SNAPSHOT_FALLBACKS), 4)
        for path, identifier in worker.SNAPSHOT_FALLBACKS.items():
            url = 'snapshot.debian.org/archive/debian/20260727T143429Z/' + path
            alternatives = [destination for expression, destination in rules if re.fullmatch(expression, url)]
            self.assertEqual(alternatives, ['https://snapshot.debian.org/file/' + identifier, 'https://' + url])
            for altered in (url.replace('20260727T143429Z', '20260728T143429Z'),
                            url.replace('_amd64.deb', '_arm64.deb'), url + '?query=1',
                            url.replace('snapshot.debian.org', 'elsewhere.example')):
                self.assertFalse(any(re.fullmatch(expression, altered) for expression, _ in rules))

    def test_selected_profile_overrides_driver_defaults_in_actual_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bazel"
            binary.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            binary.chmod(0o755)
            launcher = root / "launcher"
            for cpus, jobs, memory in ((2, 1, 4000), (4, 2, 8000)):
                with self.subTest(cpus=cpus):
                    profile = worker.select_build_profile(20 * 1024**3, cpus)
                    launcher.write_text(worker.launcher_text(str(binary), root / "output base", profile))
                    argv = json.loads(subprocess.check_output([sys.executable, str(launcher), "--nosystem_rc", "build",
                        "--jobs=6", "--local_resources=cpu=6", "--local_resources=memory=12000", "//example:image"], text=True))
                    self.assertEqual(argv[0], "--output_base=" + str(root / "output base"))
                    self.assertIn("--host_jvm_args=-Xmx3g", argv)
                    self.assertIn("--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts", argv)
                    self.assertEqual([arg for arg in argv if arg.startswith("--jobs=")][-1], "--jobs=" + str(jobs))
                    self.assertEqual([arg for arg in argv if arg.startswith("--local_resources=cpu=")][-1], "--local_resources=cpu=" + str(jobs))
                    self.assertEqual([arg for arg in argv if arg.startswith("--local_resources=memory=")][-1], "--local_resources=memory=" + str(memory))
                    self.assertIn("//example:image", argv)
                    shutdown = json.loads(subprocess.check_output([sys.executable, str(launcher), "shutdown"], text=True))
                    self.assertFalse(any(arg.startswith("--jobs=") for arg in shutdown))

    def test_phase_keeps_the_prepared_profile_and_rejects_changed_limits(self):
        profile = worker.select_build_profile(20 * 1024**3, 4)
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        state = {"build_profile": profile}
        self.assertEqual(worker.prepared_profile(state, memory), profile)
        for changed in ({**memory, "cpu_max": "200000 100000"},
                        {**memory, "limit": str(24 * 1024**3)}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "differ from preparation"):
                worker.prepared_profile(state, changed)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            args = SimpleNamespace(image="sysmgr", revision="a" * 40, phase="build", workspace=Path("/source"), artifacts=artifacts)
            state.update(image=args.image, revision=args.revision, workspace=str(args.workspace), environment={})
            (artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                    patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                worker.execute_phase(args)
            receipt = json.loads((artifacts / "worker-build.json").read_text())
            self.assertEqual(receipt["build_profile"], profile)
            self.assertEqual(receipt["status"], "passed")

    def test_only_known_images_and_full_commit_ids(self):
        for image in worker.DRIVERS:
            worker.selection(image, "a" * 40)
        for image, revision in (("$(id)", "a" * 40), ("dash-ha", "master"), ("sflow", "a" * 39), ("sysmgr", "A" * 40)):
            with self.subTest(image=image, revision=revision), self.assertRaises(ValueError):
                worker.selection(image, revision)

    def test_dedicated_checks_precede_build_and_stop_on_failure(self):
        checks = {
            "database": ("database_make_test.py",),
            "eventd": ("eventd_make_test.py",),
            "lldp": ("lldp_make_test.py", "lldp_identity_test.py"),
            "radv": ("radv_make_test.py",),
            "macsec": (),
            "mux": ("mux_make_test.py",),
        }
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        for image, scripts in checks.items():
            for failure in (None, *range(len(scripts))):
                with self.subTest(image=image, failure=failure), tempfile.TemporaryDirectory() as directory:
                    artifacts = Path(directory)
                    args = SimpleNamespace(image=image, revision="a" * 40, phase="build",
                                           workspace=Path("/source"), artifacts=artifacts)
                    state = {"image": image, "revision": args.revision, "workspace": str(args.workspace),
                             "environment": {}, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
                    (artifacts / "worker-private.json").write_text(json.dumps(state))
                    expected = [[sys.executable, "-B", "/source/tools/bazel/tests/" + script] for script in scripts]
                    expected.append(worker.phase_command(image, "build", args.workspace, artifacts))
                    results = [SimpleNamespace(returncode=0) for _ in expected]
                    if failure is not None:
                        results[failure].returncode = 7
                    with patch.object(worker, "source_check") as source_check, \
                            patch.object(worker, "memory_snapshot", return_value=memory), \
                            patch.object(worker.subprocess, "run", side_effect=results) as run:
                        if failure is None:
                            worker.execute_phase(args)
                        else:
                            with self.assertRaisesRegex(ValueError, "Image phase failed: build"):
                                worker.execute_phase(args)
                        self.assertEqual([call.args[0] for call in run.call_args_list],
                                         expected if failure is None else expected[:failure + 1])
                        self.assertEqual(source_check.call_count, 2)
                    receipt = json.loads((artifacts / "worker-build.json").read_text())
                    self.assertEqual(receipt["status"], "passed" if failure is None else "failed")
                    self.assertEqual(receipt["exit_code"], 0 if failure is None else 7)

    def test_dedicated_build_checks_do_not_repeat_during_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            args = SimpleNamespace(image="lldp", revision="a" * 40, phase="validate",
                                   workspace=Path("/source"), artifacts=artifacts)
            memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
            state = {"image": args.image, "revision": args.revision, "workspace": str(args.workspace),
                     "environment": {}, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
            (artifacts / "worker-private.json").write_text(json.dumps(state))
            with patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                    patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                worker.execute_phase(args)
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [worker.phase_command("lldp", "validate", args.workspace, artifacts)])

    def test_fixed_interfaces_do_not_expand_into_owner_suites(self):
        root, artifacts = Path("/source"), Path("/evidence")
        for image in worker.DRIVERS.keys() - {"p4rt"}:
            for phase in ("build", "validate", "incremental", "loaded"):
                command = worker.phase_command(image, phase, root, artifacts)
                self.assertEqual(command[1], "-B")
                self.assertTrue(command[2].startswith("/source/tools/bazel/ci/"))
                self.assertFalse(any(value.endswith("/...") or ".deb" in value for value in command))
                self.assertIn(str(artifacts) if image != "orchagent" or phase != "incremental" else str(artifacts / "rebuild"), command)
        with self.assertRaises(ValueError):
            worker.phase_command("sflow", "shell", root, artifacts)
        command = worker.phase_command("orchagent", "incremental", root, artifacts)
        self.assertIn("@scapy//:wheel", command)
        self.assertIn("//dockers/docker-orchagent:docker-orchagent-dbg.gz", command)
        self.assertIn("/source/tools/bazel/ci/review_bazel.sh", worker.phase_command("sysmgr", "build", root, artifacts))

    def test_review_wrapped_images_reuse_fixed_driver_phases(self):
        root, artifacts = Path("/source"), Path("/evidence")
        prefix = [sys.executable, "-B"]
        wrapper = "/source/tools/bazel/ci/review_bazel.sh"
        for image, stem in (("otel", "otel"), ("syncd", "syncd"), ("gearbox", "gbsyncd"), ("dash-engine", "dash_engine"), ("framework", "mgmt_framework"),
                            ("database", "database"), ("eventd", "eventd"), ("lldp", "lldp"),
                            ("radv", "radv"), ("macsec", "macsec"), ("mux", "mux"), ("frr", "frr")):
            with self.subTest(image=image):
                driver = f"/source/tools/bazel/ci/{stem}_image.py"
                for phase in ("build", "validate", "incremental"):
                    expected = prefix + [driver, phase, "--artifacts", str(artifacts)]
                    if phase != "validate":
                        expected += ["--bazel", wrapper]
                    self.assertEqual(worker.phase_command(image, phase, root, artifacts), expected)
                self.assertEqual(worker.phase_command(image, "loaded", root, artifacts),
                                 prefix + [f"/source/tools/bazel/ci/{stem}_loaded.py", "--artifacts", str(artifacts)])

    def test_snmp_reuses_existing_phases_and_libteam_catalog_entrypoint(self):
        root, artifacts = Path("/source"), Path("/evidence")
        for phase in ("build", "validate", "incremental"):
            command = [sys.executable, "-B", "/source/tools/bazel/ci/snmp_image.py", phase,
                       "--artifacts", str(artifacts)]
            if phase != "validate":
                command += ["--bazel", "/source/tools/bazel/ci/libteam_draft_bazel.sh"]
            self.assertEqual(worker.phase_command("snmp", phase, root, artifacts), command)
        self.assertEqual(worker.phase_command("snmp", "loaded", root, artifacts),
                         [sys.executable, "-B", "/source/tools/bazel/ci/snmp_loaded.py", "--artifacts", str(artifacts)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "tools/bazel/ci").mkdir(parents=True)
            (root / ".github/workflows/bazel-oci.yml").write_text(
                "env:\n  BAZEL: tools/bazel/ci/libteam_draft_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("snmp", root), "a" * 40)
            self.assertEqual(worker.catalog_for("snmp", root, "a" * 40), "a" * 40)
            with self.assertRaises(ValueError):
                worker.catalog_for("snmp", root, "b" * 40)
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "b" * 40 + "\n")
            with self.assertRaises(ValueError):
                worker.catalog_for("snmp", root)

    def p4rt_owner(self, workspace, temporary):
        launch, rc = temporary / "p4rt-owner-bazel", temporary / "p4rt-owner.bazelrc"
        launch.write_text("reviewed launcher"); rc.write_text("reviewed rc")
        return {"source": str(workspace / worker.P4RT_OWNER_PATH), "revision": worker.P4RT_OWNER,
                "bazel": str(launch), "bazel_sha256": worker.digest(launch), "rc": str(rc),
                "rc_sha256": worker.digest(rc), "output_base": "/bazel_cache/source-images/sonic-slave-lgh114-1/p4rt-owner/output-base",
                "jobs": 1}

    def test_p4rt_uses_exact_seven_existing_driver_interfaces(self):
        workspace, artifacts = Path("/source"), Path("/evidence")
        owner = {"source": "/source/" + worker.P4RT_OWNER_PATH, "revision": worker.P4RT_OWNER,
                 "bazel": "/private/owner-bazel", "output_base": "/cache/p4rt-owner/output-base", "jobs": 1}
        common = ["--directory", str(artifacts), "--owner-source", owner["source"]]
        prefix = [sys.executable, "-B"]
        wrapper = "/source/tools/bazel/ci/review_bazel.sh"
        phases = ("prepare-owner", "prepare-owner-runtime", "owner-tests", "build", "validate", "incremental", "loaded")
        self.assertEqual(worker.P4RT_PHASES, phases)
        for phase in phases:
            if phase == "loaded":
                expected = prefix + ["/source/tools/bazel/ci/p4rt_loaded.py", *common]
            elif phase in ("validate", "incremental"):
                expected = prefix + ["/source/tools/bazel/ci/p4rt_validate.py", phase, *common, "--bazel", wrapper]
            else:
                expected = prefix + ["/source/tools/bazel/ci/p4rt_image.py", phase, *common, "--bazel", wrapper,
                    "--owner-revision", worker.P4RT_OWNER, "--owner-bazel", owner["bazel"],
                    "--owner-output-base", owner["output_base"], "--jobs", "1"]
            self.assertEqual(worker.phase_command("p4rt", phase, workspace, artifacts, owner), expected)
        for change in ({"revision": "a" * 40}, {"source": "/other"}, {"jobs": 2}):
            with self.assertRaises(ValueError):
                worker.phase_command("p4rt", "build", workspace, artifacts, {**owner, **change})
        with self.assertRaises(ValueError):
            worker.phase_command("p4rt", "build", workspace, artifacts)
        with self.assertRaises(ValueError):
            worker.phase_command("snmp", "owner-tests", workspace, artifacts, owner)

    def test_p4rt_workflow_preparation_precedes_four_phases_and_stops_on_failure(self):
        import yaml
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        steps = workflow["jobs"]["source_image"]["steps"]
        selected = []
        for step in steps:
            match = re.search(r'source_image_worker.py" ([a-z-]+) ', step.get("run", ""))
            if match and match.group(1) in worker.P4RT_PHASES:
                selected.append((match.group(1), step))
        self.assertEqual(tuple(phase for phase, _ in selected), worker.P4RT_PHASES)
        for index, (phase, step) in enumerate(selected):
            self.assertNotIn("continue-on-error", step)
            if index < 3:
                self.assertEqual(step["if"], "inputs.source_image == 'p4rt'")
            else:
                self.assertNotIn("if", step)  # GitHub's implicit success() gate.
        self.assertEqual(workflow["jobs"]["source_image"]["timeout-minutes"], 360)
        self.assertEqual(workflow["jobs"]["source_image"]["runs-on"],
                         ["sonic-slave-container-dpool", "${{ inputs.source_worker }}"])

    def test_p4rt_owner_setup_verifies_gitlink_and_separate_cache_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);workspace = root / "source";workspace.mkdir()
            (workspace / ".bazelversion").write_text("8.5.1\n")
            owner = workspace / worker.P4RT_OWNER_PATH;owner.mkdir(parents=True)
            (owner / ".bazelversion").write_text("6.4.0\n")
            (owner / "WORKSPACE.bazel").write_text("workspace(name='owner')\n")
            (owner / "ci").mkdir();(owner / "ci/production_runtime.py").write_text("# reviewed driver\n")
            temporary = root / "scratch";temporary.mkdir();cache = root / "cache"
            def output(argv):
                if "ls-tree" in argv: return "160000 commit " + worker.P4RT_OWNER + "\t" + worker.P4RT_OWNER_PATH
                if "config" in argv: return "https://github.com/securely1g/sonic-pins.git"
                if "rev-parse" in argv: return worker.P4RT_OWNER
                if "status" in argv: return ""
                raise AssertionError(argv)
            with patch.object(worker, "output", side_effect=output), patch.object(worker, "run") as run, \
                    patch.object(worker.shutil, "which", return_value="/tools/bazelisk"):
                context = worker.prepare_p4rt(workspace, cache, temporary, [cache / "distdir"], temporary / "mirrors.cfg")
            self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(workspace), "submodule", "update", "--init", "--checkout", "--", worker.P4RT_OWNER_PATH])
            self.assertEqual(context["output_base"], str(cache / "p4rt-owner/output-base"))
            self.assertNotEqual(context["output_base"], str(cache / "p4rt/output-base"))
            rc = Path(context["rc"]).read_text()
            for option in ("common --repository_cache=", "build --disk_cache=", "build --local_ram_resources=4096", "common --distdir=", "common --experimental_downloader_config="):
                self.assertIn(option, rc)
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", [context["bazel"], "build", "//p4rt_app:p4rt_runtime"]), \
                    patch.object(os, "execv") as execute:
                exec(compile(Path(context["bazel"]).read_text(), context["bazel"], "exec"), {})
                self.assertEqual(os.environ["USE_BAZEL_VERSION"], "6.4.0")
                self.assertEqual(os.environ["BAZELISK_HOME"], str(cache / "p4rt-owner/bazelisk"))
            argv = execute.call_args.args[1]
            self.assertIn("--output_base=" + context["output_base"], argv)
            self.assertIn("--host_jvm_args=-Xmx2g", argv)
            self.assertIn("--bazelrc=" + context["rc"], argv)
            self.assertIn("--nohome_rc", argv)
            self.assertNotIn("--noworkspace_rc", argv)  # Preserve the owner's p4rt-runtime config.
            self.assertFalse(any("remote_download_regex" in x or "local_resources=" in x for x in argv))
            self.assertEqual(argv[-2:], ["build", "//p4rt_app:p4rt_runtime"])
            (workspace / ".bazelversion").write_text("6.4.0\n")
            with patch.object(worker, "run") as run, self.assertRaises(ValueError):
                worker.prepare_p4rt(workspace, cache, temporary, [], temporary / "mirror")
            run.assert_not_called()
            (workspace / ".bazelversion").write_text("8.5.1\n")
            with patch.object(worker, "output", return_value="160000 commit " + "a" * 40), \
                    patch.object(worker, "run") as run, self.assertRaises(ValueError):
                worker.prepare_p4rt(workspace, cache, temporary, [], temporary / "mirror")
            run.assert_not_called()
            (owner / ".bazelversion").write_text("8.5.1\n")
            with patch.object(worker, "output", side_effect=output), patch.object(worker, "run"), self.assertRaises(ValueError):
                worker.p4rt_owner_identity(workspace)

    def test_p4rt_publication_delegates_before_generic_directory_creation(self):
        from tools.bazel.ci import p4rt_public
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def prepare(path):
                self.assertEqual(path, root)
                self.assertFalse((root / "public").exists())
                (root / "public").mkdir()
                return root / "public"
            with patch.object(p4rt_public, "prepare", side_effect=prepare) as call:
                self.assertEqual(worker.publish(SimpleNamespace(image="p4rt", artifacts=root)), root / "public")
            call.assert_called_once_with(root)

    def test_p4rt_phase_receipts_enforce_order_and_preserve_failure(self):
        memory = {"limit": str(20 * 1024**3), "cpu_max": "400000 100000", "events": {"oom": 0, "oom_kill": 0}}
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory);artifacts = root / "artifacts";artifacts.mkdir()
                workspace = root / "source";workspace.mkdir();temporary = root / "scratch";temporary.mkdir()
                args = SimpleNamespace(image="p4rt", revision="a" * 40, workspace=workspace, artifacts=artifacts, phase="prepare-owner")
                owner = self.p4rt_owner(workspace, temporary)
                state = {"image": "p4rt", "revision": args.revision, "workspace": str(workspace), "environment": {"BAZEL_REAL": "/root-bazel"},
                         "p4rt_owner": owner, "build_profile": worker.select_build_profile(20 * 1024**3, 4)}
                (artifacts / "worker-private.json").write_text(json.dumps(state))
                def result(argv, **kwargs):
                    return SimpleNamespace(returncode=1 if fail and "owner-tests" in argv else 0)
                with patch.dict(os.environ, {"RUNNER_NAME": "sonic-slave-lgh114-1", "SOURCE_REGISTRY_REVISION": ""}), \
                        patch.object(worker, "temporary_directory", return_value=temporary), \
                        patch.object(worker, "p4rt_owner_identity", return_value=Path(owner["source"])), \
                        patch.object(worker, "source_check"), patch.object(worker, "memory_snapshot", return_value=memory), \
                        patch.object(worker.subprocess, "run", side_effect=result) as run:
                    args.phase = "build"
                    with self.assertRaises(FileNotFoundError): worker.execute_phase(args)
                    run.assert_not_called()
                    for phase in worker.P4RT_PHASES:
                        args.phase = phase
                        if fail and phase == "owner-tests":
                            with self.assertRaisesRegex(ValueError, "Image phase failed"): worker.execute_phase(args)
                            self.assertEqual(json.loads((artifacts / "worker-owner-tests.json").read_text())["status"], "failed")
                            calls = run.call_count;args.phase = "build"
                            with self.assertRaisesRegex(ValueError, "predecessor"): worker.execute_phase(args)
                            self.assertEqual(run.call_count, calls)
                            break
                        worker.execute_phase(args)
                        receipt = json.loads((artifacts / ("worker-" + phase + ".json")).read_text())
                        self.assertEqual(receipt["owner_profile"], worker.P4RT_OWNER_PROFILE)
                    if not fail:
                        self.assertEqual(run.call_count, 8)  # Seven phases and the existing root JVM shutdown.
                        self.assertEqual(run.call_args_list[-2].args[0], ["/root-bazel", "shutdown"])
                    Path(owner["rc"]).write_text("tampered")
                    args.phase = "prepare-owner"
                    with self.assertRaisesRegex(ValueError, "configuration changed"): worker.execute_phase(args)

    def test_orchagent_incremental_uses_current_source_parent_and_all_six_targets(self):
        command = worker.phase_command("orchagent", "incremental", Path("/source"), Path("/evidence"))
        self.assertEqual(command[:5], [
            sys.executable, "-B", "/source/tools/bazel/ci/swss_rebuild.py", "--bazel",
            "/source/tools/bazel/ci/libteam_draft_bazel.sh",
        ])
        self.assertEqual(list(zip(command[5::2], command[6::2])), [
            ("--runtime-target", "//dockers/docker-orchagent:docker-orchagent.gz"),
            ("--additional-runtime-target", "//dockers/docker-orchagent:docker-orchagent-dbg.gz"),
            ("--additional-runtime-target", "//dockers/docker-orchagent:rdeps"),
            ("--symbols-target", "//dockers/docker-orchagent:rdeps_debug_symbols"),
            ("--unchanged-target", "//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz"),
            ("--unchanged-target", "@scapy//:wheel"),
            ("--output", "/evidence/rebuild"),
        ])
        self.assertEqual(len(command), 19)
        self.assertFalse(any("source_config_engine" in value for value in command))

    def test_gnmi_profiles_have_fixed_flags_for_existing_four_phases(self):
        root, artifacts = Path("/source"), Path("/evidence")
        prefix = [sys.executable, "-B"]
        for image, mode in (("gnmi", "enabled"), ("gnmi-no-dialout", "disabled")):
            for phase in ("build", "validate", "incremental"):
                expected = prefix + ["/source/tools/bazel/ci/gnmi_image.py", phase,
                                      "--artifacts", str(artifacts), "--dialout", mode]
                if phase != "validate":
                    expected += ["--bazel", "/source/tools/bazel/ci/review_bazel.sh"]
                self.assertEqual(worker.phase_command(image, phase, root, artifacts), expected)
            self.assertEqual(worker.phase_command(image, "loaded", root, artifacts),
                             prefix + ["/source/tools/bazel/ci/gnmi_loaded.py", "--artifacts", str(artifacts)])
        with self.assertRaises(ValueError):
            worker.phase_command("gnmi --dialout other", "build", root, artifacts)

    def test_gnmi_publication_preserves_and_checks_selected_profile(self):
        for image, enabled in (("gnmi", True), ("gnmi-no-dialout", False)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "build-receipt.json"
                receipt = {"status": "built", "revision": "a" * 40, "architecture": "amd64",
                           "dialout_enabled": enabled, "artifacts": {}}
                path.write_text(json.dumps(receipt))
                worker.publish(SimpleNamespace(artifacts=root, image=image))
                public = json.loads((root / "public/public-receipt.json").read_text())
                self.assertIs(public["dialout_enabled"], enabled)
                for bad in (not enabled, None, int(enabled)):
                    receipt["dialout_enabled"] = bad
                    path.write_text(json.dumps(receipt))
                    with self.assertRaises(ValueError):
                        worker.publish(SimpleNamespace(artifacts=root, image=image))

    def test_workflow_allowlist_matches_fixed_driver_choices(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        choices = re.search(r"options: \[none, preflight, ([^\]]+)\]", workflow).group(1).split(", ")
        self.assertEqual(set(choices), set(worker.DRIVERS) | {"worker-provenance", "registry-native"})
        job_conditions = "\n".join(line for line in workflow.splitlines() if line.startswith("    if:"))
        gates = re.findall(r"contains\(fromJSON\('(\[[^']+\])'\), inputs.source_image\)", job_conditions)
        self.assertEqual(len(gates), 2)
        for gate in gates:
            self.assertEqual(set(json.loads(gate)), set(worker.DRIVERS))

    def test_framework_worker_pr_preserves_source_only_scope(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        branch = "codex/framework-native-worker"
        guards = [step["env"]["SOURCE_ONLY_NO_DEBS"] for job in workflow["jobs"].values()
                  for step in job.get("steps", []) if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard, event="pull_request", branch=branch))
            self.assertFalse(condition(guard, event="pull_request", branch=branch + "-suffix"))
            self.assertFalse(condition(guard, event="pull_request", branch=branch, head_repo="other/fork"))
        self.assertFalse(condition(workflow["jobs"]["vs_container"]["if"], event="pull_request", branch=branch))
        self.assertFalse(condition(workflow["jobs"]["registry_native"]["if"], event="pull_request", branch=branch))

    def test_service_dispatches_select_only_the_existing_native_image_job(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        jobs = workflow["jobs"]
        for image in ("database", "eventd", "lldp", "radv", "macsec", "mux", "frr", "snmp", "p4rt", "base-config", "swss-layer"):
            with self.subTest(image=image):
                selected = [name for name, job in jobs.items() if condition(
                    str(job.get("if", "true")).replace("inputs.source_inputs_run", "'41689621493'"), mode=image)]
                self.assertEqual(selected, ["source_image"])
                self.assertTrue(condition(jobs["source_image_inputs"]["if"], mode=image))
                self.assertFalse(condition(jobs["source_image"]["if"], mode=image))
        self.assertEqual(jobs["source_image"]["runs-on"],
                         ["sonic-slave-container-dpool", "${{ inputs.source_worker }}"])
        for image in ("snmp-other", "base", "config-engine", "swss-layer-other"):
            with self.subTest(unsupported=image), self.assertRaises(ValueError):
                worker.selection(image, "a" * 40)

    def test_worker_followups_skip_only_their_owned_deb_fixtures_and_vs(self):
        import yaml
        from tools.bazel.tests.registry_native_worker_test import condition

        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        kernel = yaml.safe_load((ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text())
        guards = [step["env"]["SOURCE_ONLY_NO_DEBS"] for job in workflow["jobs"].values()
                  for step in job.get("steps", []) if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guards), 2)
        for branch in ("codex/orchagent-source-worker-current", "codex/source-image-worker-families", "codex/source-image-worker-snmp"):
            for guard in guards:
                self.assertTrue(condition(guard, event="pull_request", branch=branch))
                for changes in ({"branch": branch + "-suffix"}, {"branch": "unrelated"},
                                {"head_repo": "other/fork"}, {"event": "push"},
                                {"event": "workflow_dispatch"}):
                    with self.subTest(branch=branch, changes=changes):
                        context = {"event": "pull_request", "branch": branch, **changes}
                        self.assertFalse(condition(guard, **context))
            for name in ("vs", "vs_container", "source_image", "source_image_inputs", "registry_native",
                         "source_worker_preflight", "worker_provenance"):
                self.assertFalse(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
            self.assertTrue(condition(workflow["jobs"]["vs_container"]["if"], event="pull_request", branch=branch + "-suffix"))
            for name in ("archive", "checks"):
                self.assertTrue(condition(workflow["jobs"][name]["if"], event="pull_request", branch=branch))
            self.assertFalse(condition(kernel["jobs"]["kernel-bundle"]["if"], event="pull_request", branch=branch))

    def test_selected_workflow_and_wrapper_catalogs_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "tools/bazel/ci").mkdir(parents=True)
            workflow = root / ".github/workflows/bazel-oci.yml"
            workflow.write_text("env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("sysmgr", root), "a" * 40)
            self.assertEqual(worker.catalog_for("otel", root), "a" * 40)
            self.assertEqual(worker.catalog_for("syncd", root), "a" * 40)
            self.assertEqual(worker.catalog_for("gearbox", root), "a" * 40)
            self.assertEqual(worker.catalog_for("dash-engine", root), "a" * 40)
            self.assertEqual(worker.catalog_for("framework", root), "a" * 40)
            self.assertEqual(worker.catalog_for("frr", root), "a" * 40)
            workflow.write_text("env:\n  BAZEL: ${{ github.workspace }}/tools/bazel/ci/libteam_draft_bazel.sh\n  SONIC_BAZEL_REGISTRY_REVISION: " + "a" * 40 + "\n")
            wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "a" * 40 + "\n")
            self.assertEqual(worker.catalog_for("dash-ha", root), "a" * 40)
            wrapper.write_text("export SONIC_BAZEL_REGISTRY_REVISION=" + "b" * 40 + "\n")
            with self.assertRaises(ValueError):
                worker.catalog_for("dash-ha", root)

    def test_macsec_catalog_comes_from_its_driver_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            drivers = root / 'tools/bazel/ci'; drivers.mkdir(parents=True)
            (workflows / 'bazel-oci.yml').write_text('env:\n  BAZEL: tools/bazel/ci/libteam_draft_bazel.sh\n')
            workflow = workflows / 'bazel-macsec.yml'
            prefix = 'env:\n  SONIC_BAZEL_REGISTRY_REVISION: '
            command = '\n  run: |\n    python3 -B tools/bazel/ci/macsec_image.py build --artifacts artifacts/macsec\n'
            driver = drivers / 'macsec_image.py'
            driver.write_text('parser.add_argument("--bazel", default="tools/bazel/ci/review_bazel.sh")\n')
            for expression in ('a' * 40, "${{ inputs.registry_revision || '" + 'a' * 40 + "' }}"):
                workflow.write_text(prefix + expression + command)
                self.assertEqual(worker.catalog_for('macsec', root), 'a' * 40)
                self.assertEqual(worker.catalog_for('macsec', root, 'b' * 40), 'b' * 40)
                for invalid in ('main', 'A' * 40, '--registry=other'):
                    with self.assertRaises(ValueError): worker.catalog_for('macsec', root, invalid)
            for expression in ('main', '${{ inputs.registry_revision }}', '${{ arbitrary.expression }}'):
                workflow.write_text(prefix + expression + command)
                with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)
            workflow.write_text(prefix + 'a' * 40 + command + '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'b' * 40 + '\n')
            with self.assertRaises(ValueError): worker.catalog_for('macsec', root)

    def test_macsec_catalog_rejects_other_entrypoints_and_commented_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            drivers = root / 'tools/bazel/ci'; drivers.mkdir(parents=True)
            (workflows / 'bazel-oci.yml').write_text('env:\n  BAZEL: bazel\n')
            workflow = workflows / 'bazel-macsec.yml'
            content = ('env:\n  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n  run: |\n'
                       '    python3 -B tools/bazel/ci/macsec_image.py build --artifacts artifacts/macsec\n')
            workflow.write_text(content)
            driver = drivers / 'macsec_image.py'
            valid = 'parser.add_argument("--bazel", default="tools/bazel/ci/review_bazel.sh")\n'
            for invalid in (valid.replace('review_bazel.sh', 'other.sh'), '# ' + valid,
                            valid.replace('"tools/bazel/ci/review_bazel.sh"', 'dynamic'), valid + valid):
                driver.write_text(invalid)
                with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)
            driver.write_text(valid)
            workflow.write_text(content.replace(' build --artifacts', ' build --bazel other --artifacts'))
            with self.assertRaises(ValueError): worker.catalog_for('macsec', root, 'b' * 40)

    def test_dedicated_service_catalogs_preserve_combined_source_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / '.github/workflows'; workflows.mkdir(parents=True)
            common = workflows / 'bazel-oci.yml'
            for image, filename in worker.SOURCE_WORKFLOWS.items():
                common.write_text('env:\n  BAZEL: bazel\n')
                dedicated = workflows / filename
                dedicated.write_text('env:\n  BAZEL: ./tools/bazel/ci/review_bazel.sh\n'
                                     '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n')
                self.assertEqual(worker.catalog_for(image, root), 'a' * 40)
                self.assertEqual(worker.catalog_for(image, root, 'b' * 40), 'b' * 40)
                common.write_text('env:\n  BAZEL: tools/bazel/ci/review_bazel.sh\n'
                                  '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'c' * 40 + '\n')
                self.assertEqual(worker.catalog_for(image, root), 'c' * 40)
                self.assertEqual(worker.catalog_for(image, root, 'b' * 40), 'b' * 40)
                common.write_text('env:\n  BAZEL: bazel\n')
                dedicated.write_text('env:\n  BAZEL: bazel\n'
                                     '  SONIC_BAZEL_REGISTRY_REVISION: ' + 'a' * 40 + '\n')
                if image == 'macsec':
                    (root / 'tools/bazel/ci').mkdir(parents=True)
                    (root / 'tools/bazel/ci/macsec_image.py').write_text('')
                with self.assertRaises(ValueError): worker.catalog_for(image, root, 'b' * 40)
            self.assertNotIn('database', worker.SOURCE_WORKFLOWS)
            with self.assertRaises(ValueError): worker.catalog_for('database', root, 'b' * 40)

    def test_source_download_handoff_rejects_changed_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, (version, _) in worker.SOURCES.items():
                archive = root / "inputs" / (name + "-" + version) / "src.tar.gz"
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"changed source archive")
            receipt = [{"module": name, "version": version, "integrity": integrity,
                        "bytes": len(b"changed source archive"),
                        "archive": "artifacts/source-image-inputs/" + name + "-" + version + "/src.tar.gz"}
                       for name, (version, integrity) in worker.SOURCES.items()]
            (root / "inputs/receipt.json").write_text(json.dumps(receipt))
            worker.check_source_inventory(root / "inputs", False)
            with self.assertRaises(subprocess.CalledProcessError):
                worker.prepare_source_downloads(root / "inputs", root / "cache")
            self.assertFalse((root / "cache").exists())

    def test_reused_input_run_requires_successful_same_repository_producer(self):
        selected = {"repository": {"full_name": "securely1g/sonic-buildimage", "id": 99},
                    "path": ".github/workflows/bazel-oci.yml", "name": "Bazel OCI", "head_sha": "a" * 40}
        jobs = {"total_count": 1, "jobs": [{"id": 2, "name": "OCI regression checks (AMD64)", "conclusion": "success",
                "steps": [{"name": name, "conclusion": "success"} for name in
                          ("Retain the verified GitLab source downloads", "Upload the source downloads for the VS job")]}]}
        artifacts = {"total_count": 1, "artifacts": [{"id": 3, "name": "bazel-repository-inputs", "expired": False,
                     "size_in_bytes": 123, "workflow_run": {"id": 1, "head_sha": "a" * 40, "repository_id": 99}}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "SOURCE_INPUTS_RUN": "1", "GH_TOKEN": "test-only", "GITHUB_REPOSITORY": "securely1g/sonic-buildimage"}):
            args = SimpleNamespace(artifacts=Path(directory))
            for kind in ("valid", "foreign repository", "failed job", "expired artifact"):
                records = copy.deepcopy([selected, jobs, artifacts])
                if kind == "foreign repository": records[0]["repository"]["full_name"] = "elsewhere/repo"
                if kind == "failed job": records[1]["jobs"][0]["conclusion"] = "failure"
                if kind == "expired artifact": records[2]["artifacts"][0]["expired"] = True
                with self.subTest(kind=kind), patch.object(worker.urllib.request, "urlopen",
                        side_effect=[io.StringIO(json.dumps(record)) for record in records]):
                    if kind == "valid":
                        worker.verify_input_run(args)
                        self.assertEqual(json.loads((args.artifacts / "worker-input-run.json").read_text())["artifact"], 3)
                    else:
                        with self.assertRaises(ValueError): worker.verify_input_run(args)

    def test_handoff_rejects_extra_missing_and_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = []
            for name, (version, integrity) in worker.SOURCES.items():
                relative = name + "-" + version + "/src.tar.gz"
                archive = root / relative
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"fixture")
                receipt.append({"module": name, "version": version, "integrity": integrity, "bytes": 7,
                                "archive": "artifacts/source-image-inputs/" + relative})
            (root / "receipt.json").write_text(json.dumps(receipt))
            worker.check_source_inventory(root, False)
            (root / "extra").write_text("unexpected")
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            (root / "extra").unlink()
            archive.unlink()
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            archive.symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                worker.check_source_inventory(root, False)
            with patch.dict(os.environ, {"SOURCE_INPUTS_RUN": "../41689621493"}), self.assertRaises(ValueError):
                worker.input_run_id()

    def test_publication_preserves_exact_checked_syncd_and_gearbox_policy_bytes(self):
        cases = ({"DEBIAN_FRONTEND": "noninteractive"}, {"DEBIAN_FRONTEND": "interactive"},
                 {"DEBIAN_FRONTEND": "noninteractive", "UNRELATED": "value"})
        for image in ('syncd', 'gearbox'):
            for environment in cases:
                with self.subTest(image=image, environment=environment), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy = root / 'policy.json'
                    policy.write_text(json.dumps({'image_validation': {'environment': environment}}))
                    original = policy.read_bytes()
                    (root / 'build-receipt.json').write_text(json.dumps({'status': 'built', 'artifacts': {
                        policy.name: {'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}}}))
                    args = SimpleNamespace(artifacts=root, image=image)
                    if environment == {"DEBIAN_FRONTEND": "noninteractive"}:
                        worker.publish(args)
                        self.assertEqual((root / 'public/policy.json').read_bytes(), original)
                        self.assertEqual(policy.read_bytes(), original)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Unexpected image policy environment'):
                            worker.publish(args)
                        self.assertFalse((root / 'public/policy.json').exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'policy.json'
            policy.write_text(json.dumps({'image_validation': {'environment': cases[0]},
                                          'diagnostic': {'environment': {'UNRELATED': 'value'}}}))
            (root / 'build-receipt.json').write_text(json.dumps({'artifacts': {policy.name: {
                'bytes': policy.stat().st_size, 'sha256': worker.digest(policy)}}}))
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root, image='syncd'))
            self.assertFalse((root / 'public/policy.json').exists())

    @staticmethod
    def snmp_validation_receipt():
        return {"status": "passed", "manifest_digest": "sha256:" + "a" * 64,
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "b" * 64]},
                "wheel_payloads": {},
                "config": {"Entrypoint": ["/usr/bin/docker-snmp-init.sh"], "WorkingDir": "/",
                           "Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                   "DEBIAN_FRONTEND=noninteractive", "PYTHONOPTIMIZE=1",
                                   "IMAGE_VERSION=bazel-snmp-ci"]}}

    def test_publication_preserves_exact_checked_snmp_validation_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (json.dumps(self.snmp_validation_receipt(), indent=3) + "\n").encode()
            for name in ("docker-snmp.validation.json", "docker-snmp-dbg.validation.json"):
                (root / name).write_bytes(original)
            with self.assertRaisesRegex(ValueError, "diagnostic-json-field"):
                worker.safe_json(json.loads(original))
            for _ in range(2):
                worker.publish(SimpleNamespace(artifacts=root, image="snmp"))
                for name in ("docker-snmp.validation.json", "docker-snmp-dbg.validation.json"):
                    self.assertEqual((root / name).read_bytes(), original)
                    self.assertEqual((root / "public" / name).read_bytes(), original)
                    self.assertEqual(worker.digest(root / name), worker.digest(root / "public" / name))

    def test_snmp_publication_rejects_unexpected_environment_and_private_fields(self):
        cases = []
        for bad in (None, [], ["PATH=/private"],
                    self.snmp_validation_receipt()["config"]["Env"] + ["UNRELATED=value"],
                    self.snmp_validation_receipt()["config"]["Env"] + ["TOKEN=private"],
                    {"DEBIAN_FRONTEND": "noninteractive"}):
            value = self.snmp_validation_receipt()
            value["config"]["Env"] = bad
            cases.append(("snmp", "docker-snmp.validation.json", value))
        for field, child in (("environment", {"PRIVATE": "value"}),
                             ("authorization", "private"), ("note", "ghp_" + "a" * 32)):
            value = self.snmp_validation_receipt()
            value["config"][field] = child
            cases.append(("snmp", "docker-snmp-dbg.validation.json", value))
        value = self.snmp_validation_receipt()
        value["diagnostic"] = {"Env": ["PRIVATE=value"]}
        cases.append(("snmp", "docker-snmp.validation.json", value))
        cases.extend((image, name, self.snmp_validation_receipt()) for image, name in (
            ("frr", "docker-snmp.validation.json"), (None, "docker-snmp.validation.json"),
            ("snmp", "other.validation.json"), ("snmp", "docker-snmp.runtime.json")))
        for image, name, value in cases:
            with self.subTest(image=image, name=name, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = json.dumps(value).encode()
                (root / name).write_bytes(original)
                with self.assertRaises(ValueError):
                    worker.publish(SimpleNamespace(artifacts=root, image=image))
                self.assertEqual((root / name).read_bytes(), original)
                self.assertFalse((root / "public" / name).exists())

    def test_publication_keeps_declared_bytes_and_excludes_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "image.gz"
            payload.write_bytes(b"verified archive fixture")
            receipt = {"status": "built", "revision": "a" * 40,
                       "invocation": {"environment": {"private": "not for publication"}},
                       "artifacts": {payload.name: {"bytes": payload.stat().st_size,
                                                    "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}}}
            (root / "build-receipt.json").write_text(json.dumps(receipt))
            (root / "worker-private.json").write_text('{"environment":{"private":"not for publication"}}')
            (root / "dockerd.log").write_text("private daemon diagnostic")
            (root / "worker-build.json").write_text('{"status":"passed"}')
            worker.publish(SimpleNamespace(artifacts=root))
            worker.publish(SimpleNamespace(artifacts=root))
            public = root / "public"
            self.assertEqual((public / "image.gz").read_bytes(), payload.read_bytes())
            self.assertFalse((public / "worker-private.json").exists())
            self.assertFalse((public / "dockerd.log").exists())
            self.assertNotIn("invocation", json.loads((public / "public-receipt.json").read_text()))
            payload.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root))

    def test_publication_retains_checked_loaded_validation_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build-receipt.json").write_text('{"artifacts":{}}')
            receipt = {"status": "passed", "images": {"runtime": {"status": "passed"},
                                                      "debug": {"status": "passed"}}}
            source = root / "loaded-validation.json"
            source.write_text(json.dumps(receipt))
            worker.publish(SimpleNamespace(artifacts=root))
            self.assertEqual(json.loads((root / "public/loaded-validation.json").read_text()), receipt)
            source.write_text(json.dumps({**receipt, "environment": {"TOKEN": "private"}}))
            with self.assertRaises(ValueError):
                worker.publish(SimpleNamespace(artifacts=root))
            self.assertEqual(json.loads((root / "public/loaded-validation.json").read_text()), receipt)


if __name__ == "__main__":
    unittest.main()
