#!/bin/bash
set -euo pipefail

cache=${SONIC_DPKG_CACHE_SOURCE:?}
mode=${SONIC_DPKG_CACHE_METHOD:?}
case "$mode" in
    rcache|rwcache) ;;
    *) printf 'Unsupported NFS package cache mode: %s\n' "$mode" >&2; exit 1 ;;
esac
if [[ ! -d "$cache" || ! -r "$cache" || ! -x "$cache" ]]; then
    printf 'NFS package cache is missing or inaccessible: %s\n' "$cache" >&2
    exit 1
fi
filesystem=$(findmnt --noheadings --raw --output FSTYPE --target "$cache")
case "$filesystem" in
    nfs|nfs4) ;;
    *) printf 'Package cache must be NFS-backed, found %s: %s\n' "$filesystem" "$cache" >&2; exit 1 ;;
esac
if [[ "$mode" == rwcache && ! -w "$cache" ]]; then
    printf 'NFS package cache is not writable for push builds: %s\n' "$cache" >&2
    exit 1
fi
printf 'Validated NFS package cache: %s (mode=%s)\n' "$cache" "$mode"
