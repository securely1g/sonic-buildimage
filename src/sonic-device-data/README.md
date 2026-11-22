# sonic-device-data
Device-specific data for the SONiC project

See the [SONiC Website](https://sonic-net.github.io/SONiC/) for more information about the SONiC project.

## AMD64 VS installer

Build the device data used by the AMD64 VS installer with
`bazel build //src/sonic-device-data:vs_runtime_tar`. The output is
`bazel-bin/src/sonic-device-data/vs_runtime_tar.tar`. It contains the device
tree, including generated virtual HwSKU profiles, and the package documentation.
The same configuration checks used by Make run before Bazel archives the files.

When several physical platforms define the same HwSKU, the virtual platform
needs one source for its profile. The Bazel target uses lexical path order,
with the choices in `prepare_vs.py` preserving the installed profiles from the
[reference VS installer](https://github.com/securely1g/sonic-buildimage/pull/251).
Installed files use Git executable modes; stage an executable-bit change with
`git add` before building a modified checkout.
