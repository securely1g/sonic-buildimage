# Validate a built virtual-switch guest

The VS image builder already installs and boots its QCOW2 disk under QEMU/KVM,
then logs in through `check_install.py`. `VS_VALIDATE_SERVICES=y` adds strict
checks to that same guest session. The full VS GitHub Actions job enables it.
Ordinary builds retain the existing behavior unless this Make option is set.

The checks wait up to ten minutes for the single-ASIC guest's `database`,
`swss`, `syncd` and `bgp` services. Each must have an active systemd unit,
a running Docker container and every declared critical process running.
Critical `program:` and `group:` declarations follow the
[sonic-mgmt service checks](https://github.com/sonic-net/sonic-mgmt/blob/3db30429f33b287bab77c0e7d515f122cd263d9e/tests/common/devices/sonic.py#L508).
A missing declaration, missing program or failed Redis `PING` fails validation.
The probe only reads guest state; it starts no services or host processes.

The builder writes `target/sonic-vs.guest-validation.json` alongside the disk.
It includes each attempt, the guest version and the raw QCOW2 SHA256. CI checks
that the uploaded `sonic-vs.img.gz` decompresses to that same disk and retains
the receipt as `sonic-vs-guest-validation-amd64`. A missing or failed receipt
fails the full VS job. The image builder cleans up its own QEMU child on either
success or failure. Existing login and `show version` output stays in the build
log.

## What this proves

A successful full VS job proves that its exact image boots, accepts the existing
installer login and runs those core services and their critical processes.
Hosted helper tests validate failure handling and receipt matching; they do
not boot a guest. Actual guest execution requires the existing KVM runner.
This check currently targets the single-ASIC AMD64 VS image.

## Forwarding requires a data-plane testbed

The installer QEMU command creates one user-network management NIC. It has no
front-panel links or packet-test peer. Guest loopback traffic or a management
ping would not prove SONiC data-plane forwarding.

Use SONiC's existing [VS testbed setup](https://github.com/sonic-net/sonic-mgmt/blob/3db30429f33b287bab77c0e7d515f122cd263d9e/docs/testbed/README.testbed.VsSetup.md)
with a dedicated T0 topology, the image under test, its inventory and a PTF
packet-test host. After deploying the topology and its normal configuration,
a bounded existing test is:

```sh
cd sonic-mgmt/tests
./run_tests.sh -n vms-kvm-t0 -d vlab-01 \
  -c route/test_static_route.py::test_static_route \
  -f vtestbed.yaml -i ../ansible/veos_vtb
```

Those names are the documented sample testbed, not automatic discovery. The
[existing test](https://github.com/sonic-net/sonic-mgmt/blob/3db30429f33b287bab77c0e7d515f122cd263d9e/tests/route/test_static_route.py#L541)
programs a route and verifies packets through PTF interfaces, then removes the
route. Run it only against the dedicated testbed prepared for that image.
Keep its testbed/source revisions, image checksum and pytest result alongside
the guest receipt. No forwarding pass is claimed until that testbed run succeeds.
