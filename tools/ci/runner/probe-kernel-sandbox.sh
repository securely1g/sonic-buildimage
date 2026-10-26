#!/usr/bin/env bash
set -u
if ! getent group gsonic-runner >/dev/null; then groupadd -g 1003 gsonic-runner; fi
if ! id sonic-runner >/dev/null 2>&1; then
  useradd -m -u 1001 -g 1003 -d /var/sonic-runner sonic-runner
fi
printf '\nContainer policy:\n'
cat /proc/self/attr/current
for name in kernel/apparmor_restrict_unprivileged_userns kernel/unprivileged_userns_clone user/max_user_namespaces; do
  if [[ -f /proc/sys/$name ]]; then printf '%s=' "$name"; cat "/proc/sys/$name"; fi
done
printf '\nKernel build state:\n'
find /sonic/target/bazel-kernel-state -maxdepth 5 -type d -name execroot -print
printf '\nSandbox executables:\n'
mapfile -t sandboxes < <(find /sonic/target/bazel-kernel-state /bazel_cache/kernel-linux-sandbox /bazel_cache/bazelisk -type f -name linux-sandbox 2>/dev/null)
printf '%s\n' "${sandboxes[@]}"
for sandbox in "${sandboxes[@]}"; do
  stat -- "$sandbox"
  file -- "$sandbox"
  sha256sum -- "$sandbox"
  findmnt -no TARGET,FSTYPE,OPTIONS --target "$sandbox"
  execution_root=$(find /sonic/target/bazel-kernel-state -maxdepth 7 -type d -path '*/execroot/*' -print -quit)
  if [[ -z $execution_root ]]; then execution_root=/sonic; fi
  printf '\nProbe cwd=%s sandbox=%s\n' "$execution_root" "$sandbox"
  runuser -u sonic-runner -- bash -c '
    cd "$1" || exit
    id
    cat /proc/self/attr/current
    awk "/^(Uid|Gid|CapInh|CapPrm|CapEff|CapBnd|NoNewPrivs|Seccomp):/" /proc/self/status
    timeout 30s env -i "$2" -T 15 -D /tmp/sonic-sandbox-probe.log -- /bin/true
    printf "sandbox_exit=%s\n" "$?"
    cat /tmp/sonic-sandbox-probe.log
    timeout 20s unshare --user --map-root-user --mount /bin/true
    printf "user_mount_namespace_exit=%s\n" "$?"
  ' probe "$execution_root" "$sandbox"
done
printf '\nRelevant host kernel policy messages:\n'
dmesg --time-format iso --since '15 minutes ago' | grep -Ei 'apparmor|denied|userns|namespace|sandbox' | tail -100
test "${#sandboxes[@]}" -gt 0
