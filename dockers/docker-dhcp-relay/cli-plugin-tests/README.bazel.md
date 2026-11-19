# DHCP relay CLI tests

`//dockers/docker-dhcp-relay/cli-plugin-tests:upstream_tests` executes all eight
existing CLI test files and their original mocks. This preserves the checks in
the legacy Docker recipe when Make selects the Bazel image path.

Run the target in the matching Trixie environment. It uses Common's existing
native test runtime, the published SONiC utilities wheel, and config-engine's
already locked Make SDK source and MockRedis inputs. The five additional test
imports have a separate lock; none is installed into the DHCP image by this
target. Tests run against a private installed utilities wheel and writable
copies of declared CLI fixtures, retaining pytest.ini's coverage reports.

Update the additional test lock with uv 0.12.18 and a Python 3.13 interpreter:

```sh
uv pip compile dockers/docker-dhcp-relay/cli-plugin-tests/requirements.in \
  --python-version 3.13 --generate-hashes --no-emit-index-url \
  --output-file dockers/docker-dhcp-relay/cli-plugin-tests/requirements_lock.txt
```

The standard rules_python source-wheel handling uses the matching runner's GCC
for the external Netifaces test dependency, as config-engine already does for
its source-only external wheels. No DEB package is generated.
