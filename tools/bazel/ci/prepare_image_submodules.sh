#!/usr/bin/env bash
set -euo pipefail

# Only gNOI is required in a fresh checkout. Reused Actions checkouts can also
# retain other populated submodules at revisions from the previous job.
{
    printf '%s\0' src/sonic-sysmgr/gnoi
    git submodule foreach --quiet 'printf "%s\0" "$sm_path"'
} | xargs -0 git --literal-pathspecs submodule update --init --checkout --

# Restore populated nested modules without initializing unused nested sources.
git submodule foreach --recursive \
    'git -c safe.directory="$toplevel" -c safe.directory="$toplevel/$sm_path" -C "$toplevel" submodule update --checkout -- "$sm_path"'
