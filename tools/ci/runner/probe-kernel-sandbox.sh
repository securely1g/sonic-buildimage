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
printf '\nFresh reproduction: same builder, Bazel release and user; historical workspace was removed.\n'
curl --fail --location --retry 3 https://releases.bazel.build/8.5.1/release/bazel-8.5.1-linux-x86_64 -o /tmp/bazel-8.5.1
printf '%s  %s\n' 61d89402f0368e64b6c827be5de79d8e65382e8124c3cbb97325611a1851392e /tmp/bazel-8.5.1 | sha256sum --check
chmod 0755 /tmp/bazel-8.5.1
output=/sonic/target/bazel-kernel-state/probe/build/output
mkdir -p "$output/probe/execroot/_main"
chown -R 1001:1003 /sonic/target
runuser -u sonic-runner -- /tmp/bazel-8.5.1 --batch --output_user_root="$output" --nosystem_rc --nohome_rc version
printf '\nSandbox executables:\n'
mapfile -t sandboxes < <(find "$output/install" -type f -name linux-sandbox)
printf '%s\n' "${sandboxes[@]}"
for sandbox in "${sandboxes[@]}"; do
  stat -- "$sandbox"
  file -- "$sandbox"
  sha256sum -- "$sandbox"
  findmnt -no TARGET,FSTYPE,OPTIONS --target "$sandbox"
  execution_root=$output/probe/execroot/_main
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
