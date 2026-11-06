# Final VS archive binding

The default `vs_archive_reuse.py` commands retain the existing two-family,
schema-1 contract for Orchagent and Syncd. The opt-in `final23` profile covers
20 runtime families and their three shared parents on native amd64/Trixie/VS.
The declarations are in `vs_final23.py`.

Activation depends on the pending owner integrations. On the maintained review
base, the SWSS parent and these runtime families still lack their Bazel BUILD
files: DASH HA, Database, Eventd, FRR, LLDP, MACsec, MUX, router-advertiser,
sFlow, SNMP, gNMI, management framework, OTEL, P4RT, Teamd, DASH-engine and
Gearbox. Track their source and Docker integrations in
[the migration plan](https://github.com/securely1g/sonic-buildimage/issues/88).
The local-package channel also requires
[the MACsec handoff fix](https://github.com/securely1g/sonic-buildimage/pull/177).
The profile cannot pass on that base until those prerequisites are present;
its fixture tests do not claim full-image acceptance.

For a full-scope build, pass `SONIC_BAZEL_VS_ARCHIVE_PROFILE=final23` to the
existing Make invocation. Keep every in-scope service enabled and the reviewed
Bazel/FIPS/DASH, non-ASAN/non-RPC profile. This adds the existing base,
config-engine and SWSS-parent archive export targets; it reuses their producer
graphs and caches. It does not build another image implementation.

After `build_image.sh` succeeds, Make records its resolved installer and local
package selections in `target/sonic-vs.bin.selection.json`. The companion
`.dockers` file remains unchanged. The receipt includes the actual image version,
eventd setting and debug choice. This Make hook only records resolved values;
profile, inclusion and archive checks run in the separate capture/verify steps.
MACsec stays in `SONIC_PACKAGES_LOCAL`; its
archive has a `latest` tag and the existing local installer handles the resolved
image-version tag. Archive package versions remain the Make manifest versions.

Capture before running post-build validation, then verify afterward:

```sh
python3 -B tools/bazel/ci/vs_archive_reuse.py capture-pairs \
  --profile final23 --workspace "$PWD" --receipt artifacts/vs/final23.json
# Run the existing owner, loaded-image and post-build checks.
python3 -B tools/bazel/ci/vs_archive_reuse.py verify-pairs \
  --profile final23 --workspace "$PWD" --receipt artifacts/vs/final23.json
```

Add `--debug-installer` to capture when Make selected debug images. This is an
explicit opt-in interface; existing CI has not been switched to it.

The new schema-2 receipt binds 42 archives: three parents, 20 runtime images,
and 19 debug side outputs. It checks platform, exact tags, manifests, parent and
debug layer ancestry, inherited configuration and resolved environment inputs.
DASH-engine remains runtime-only in the installer; its separate image driver
owns its validation-only debug variant. P4RT's intermediate symbol layer is a
valid extension of the exact runtime prefix.

The eight deferred families keep their existing Make selections. Selected
legacy archives and available producer stamps are hashed and must remain
unchanged after validation; they do not gain a new Bazel migration requirement.
A missing in-scope family, unexpected channel, unknown family, builder fallback,
changed input, or changed archive fails the final23 contract.

These checks bind archives to the successful Make selection. They do not replace
owner payload/ELF tests, service probes, loaded-image checks, or installer boot
checks, and do not themselves establish full VS image acceptance.
