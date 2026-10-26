#!/usr/bin/env bash
set -euo pipefail
# Run as the normal build user inside the selected SONiC builder.
# Reuse the verified install base covered by the temporary AppArmor allowance.
# The fixture requires system Python only, and rejects repository downloads.
probe_bazel=${1:?absolute path to the pinned Bazel 8.5.1 executable}
probe_install=${2:?absolute path to its verified existing install base}
probe_root=${3:?fresh dedicated directory, preferably under /sonic/target}
probe_fixture=$(cd -- "$(dirname -- "$0")/source" && pwd)
test "$(id -u)" -ne 0
test ! -e "$probe_root"
mkdir -p "$probe_root/source" "$probe_root/outside"
cp "$probe_fixture/probe.bzl" "$probe_fixture/probe.py" "$probe_root/source/"
printf 'module(name = "sonic_sandbox_probe")\n' > "$probe_root/source/MODULE.bazel"
/usr/bin/python3 - "$probe_root" <<'PY'
from pathlib import Path
import os
import sys
p = Path(sys.argv[1]).resolve()
guard = p / 'outside/guard'
guard.write_text('guard unchanged\n')
assert os.access(guard, os.W_OK)
(p / 'source/BUILD.bazel').write_text(
    'load(":probe.bzl", "sandbox_probe")\n'
    'platform(name = "probe_platform")\n'
    'sandbox_probe(name="probe", guard=' + repr(str(guard)) +
    ', parent_netns=' + repr(os.readlink('/proc/self/ns/net')) + ')\n')
PY
cd "$probe_root/source"
"$probe_bazel" --batch --install_base="$probe_install" \
  --output_user_root="$probe_root/output" --nosystem_rc --nohome_rc --noworkspace_rc \
  build //:probe --incompatible_autoload_externally= \
  --host_platform=//:probe_platform --platforms=//:probe_platform \
  --strategy=SonicKernelBuild=linux-sandbox --sandbox_default_allow_network=false \
  --repository_disable_download --lockfile_mode=off --disk_cache= --remote_cache= \
  --execution_log_json_file="$probe_root/execution.json" \
  --sandbox_debug --verbose_failures
cat bazel-bin/probe.json
/usr/bin/python3 - "$probe_root/execution.json" "$probe_root/outside/guard" <<'PY'
import json
from pathlib import Path
import sys
raw = Path(sys.argv[1]).read_text()
decoder = json.JSONDecoder()
records = []
while raw.strip():
    record, end = decoder.raw_decode(raw.lstrip())
    records.append(record)
    raw = raw.lstrip()[end:]
actions = [r for r in records if r.get('mnemonic') == 'SonicKernelBuild']
assert len(actions) == 1, actions
assert actions[0]['runner'] == 'linux-sandbox', actions
assert actions[0].get('exitCode', 0) == 0, actions
assert not actions[0].get('cacheHit', False), actions
assert Path(sys.argv[2]).read_bytes() == b'guard unchanged\n'
print('Verified one real uncached linux-sandbox action; guard unchanged.')
PY
