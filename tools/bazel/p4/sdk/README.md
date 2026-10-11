# Cached P4/DASH SDK for source-built clients

`dash_sai` is a public C++ dependency for clients built from source. It links
the exact cached `libsai` and PI libraries recorded in `../packages.lock.json`
with Debian's compatible Protobuf 32 and gRPC 1.51 libraries. `sai_headers`
exposes the headers from that lock's `libsai-dev` package. Keeping the header
target separate lets sairedis retain its own declared SAI header revision.

The inputs remain imported AMD64 Trixie packages. These targets do not compile
PI, BMv2, P4C or DASH SAI and do not produce DEBs. Standard `http_file` verifies
the original package hashes; declared BSD tar actions extract their payloads;
public `cc_import` targets expose unchanged library bytes.

For a sairedis revision supporting DASH runtime packaging:

```sh
bazel test --@sonic_sairedis//:enable_dashsai=true \
  --@sonic_sairedis//:dash_sai=//tools/bazel/p4/sdk:dash_sai \
  //tools/bazel/p4/sdk:sdk_runtime_test \
  //tools/bazel/p4/sdk:packages_test
```

Run in native AMD64 Trixie with the repository's declared toolchain and native
binutils and GDB installed. The runtime test executes the cached DASH library's
SAI version API through a newly compiled client and compares its installed
header version. The package test checks both
source-built Syncd variants, their selected SAI implementations, VS SAI and
matching detached symbols, including source-line lookup through installed
build-ID paths. Redis-backed service behavior and packet forwarding remain
downstream checks.

When changing the approved binary tuple, review the library SONAMEs, Debian
ABI dependencies and the explicit public-header inventory together. Matching
package version strings alone do not establish compatibility.

## Draft source overrides

The committed registry configuration stays on SONiC registry `main` and BCR,
with existing registered module versions. While prerequisites are Draft, add
explicit `--override_module` flags for the source candidates when running the
SDK checks. The scoped workflow prepares and pins these inputs:

- [sairedis #3](https://github.com/securely1g/sonic-sairedis/pull/3), source
  `b5a8f7e94d35007a4ce57238064346977b12ea0c`.
- Upstream Distroless 0.9.4, archive SHA-256
  `5d43f4f44f0f0fb614194a07a6dcf7b29338cfc8ab7f6129ad1e4403c1ad1c73`,
  with the published Protobuf header fix and
  [infra #33](https://github.com/securely1g/sonic-build-infra/pull/33)'s parser
  patch from source `7755cb7ee3efd0eb97d16721bb64c070141f6e96`.

See `.github/workflows/bazel-p4-sdk.yml` for reproducible source preparation.
After [registry #54](https://github.com/securely1g/sonic-bazel-registry/pull/54)
and [registry #56](https://github.com/securely1g/sonic-bazel-registry/pull/56)
land with merged-source provenance, update the registered versions and remove
the temporary source overrides before declaring this consumer ready.
