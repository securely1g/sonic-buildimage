# Bazel Python package tests

The integration checks run the complete config-engine and py-common pytest
directories, focused native imports and command checks, template rendering,
and installation of both generated wheels. Existing component tests remain
under their source directories. The native AMD64/ARM64 workflow retains the
wheels, hashes, test logs/XML, collected inventories and installation receipt.

```sh
bazel test \
  //tools/bazel/tests:sonic_config_engine_full_test \
  //tools/bazel/tests:sonic_py_common_full_test \
  //tools/bazel/tests:sonic_python_wheels_test \
  //tools/bazel/tests:sonic_py_common_test \
  //src/sonic-config-engine:sonic_cfggen_cli_test \
  //tools/bazel/tests:cfggen_template_test
```

The self-contained helper tests exercise evidence collection and archive
publication, including failure paths. Run them with:

```sh
python3 -B -m unittest discover -s tools/bazel/tests -p '*_test.py'
```
