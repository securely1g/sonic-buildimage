# Shared Bazel CI helpers

The Python package CI exports wheels with `build.py` and retains command logs
with `command_log.py`. `python_packages.py` requires complete source-suite
logs, XML, collected test inventories and installed-wheel receipts. It checks
wheel hashes, native architecture and Python version. Artifact hashes use
`artifact_validation.py`, and publication uses `tools/bazel/build_helpers.py`.

Run the helper unit tests from the repository root:

```sh
python3 -B -m unittest discover -s tools/bazel/tests -p '*_test.py'
```
