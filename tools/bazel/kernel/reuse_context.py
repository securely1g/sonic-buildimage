"""Verify existing kernel outputs against an explicitly pinned producer context.

The caller must obtain the context SHA256 from its reviewed controller inputs,
not from the package artifact or from the context file itself. This module does
not produce packages, change original metadata, or write an acceptance stamp.
"""

import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys


SETTINGS = frozenset((
    "CONFIGURED_ARCH", "CONFIGURED_PLATFORM", "BLDENV", "CROSS_BUILD_ENVIRON",
    "MULTIARCH_QEMU_ENVIRON", "KERNEL_VERSION", "KERNEL_ABISUFFIX",
    "KERNEL_SUBVERSION", "KERNEL_FEATURESET", "KVERSION", "ADDITIONAL_BUILD_PROFILES",
    "SECURE_UPGRADE_MODE", "SECURE_UPGRADE_KERNEL_CAFILE", "INCLUDE_EXTERNAL_PATCHES",
    "EXTERNAL_KERNEL_PATCH_URL", "EXTERNAL_KERNEL_PATCH_LOC",
))
REVIEWABLE_DIFFERENCES = {
    ".gitmodules": "non-kernel-source-url",
    "Makefile.work": "reuse-only-consumer",
    "build_debian.sh": "reuse-only-consumer",
    "slave.mk": "reuse-only-consumer",
    "rules/linux-kernel.mk": "verify-only-integration",
    "tools/bazel/kernel/prepare.py": "verify-only-integration",
    "tools/bazel/kernel/reuse_context.py": "verify-only-integration",
    "tools/bazel/kernel/README.md": "documentation",
    "tools/bazel/kernel/.bazelrc": "download-transport",
    "tools/bazel/debian_mirrors.cfg": "download-transport",
}
PRODUCTION_FIELDS = (
    "kernel_gitlink", "source_tree_sha256", "source_archives", "modules", "launcher",
    "kernel_config",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular_bytes(path, maximum=None):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "expected a regular context input")
    if maximum is not None:
        require(0 < path.stat().st_size <= maximum, "context input exceeds its bound")
    return path.read_bytes()


def strict_json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("nonfinite JSON value")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def load_context(path, expected_sha256):
    require(isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
            "a separately pinned context SHA256 is required")
    data = regular_bytes(path, 256 * 1024)
    require(digest(data) == expected_sha256, "producer context SHA256 differs")
    value = strict_json(data)
    require(isinstance(value, dict) and set(value) == {
        "schema", "producer", "consumer", "catalog", "differences"}, "unknown context fields")
    require(type(value["schema"]) is int and value["schema"] == 1, "unknown context schema")
    for name, keys in (
        ("producer", {"commit", "prepare_sha256", "inputs_sha256", "registry",
                      "manifest_sha256", "provenance_sha256"}),
        ("consumer", {"commit", "inputs_sha256", "registry"}),
        ("catalog", {"commit"}),
    ):
        fields = value[name]
        require(isinstance(fields, dict) and set(fields) == keys, "unknown " + name + " fields")
        for key in keys:
            require(isinstance(fields[key], str), "context values must be strings")
            if key == "commit" or key.endswith("sha256"):
                length = 40 if key == "commit" else 64
                require(re.fullmatch(r"[0-9a-f]{%d}" % length, fields[key]), "invalid context identity")
    require(isinstance(value["differences"], list) and len(value["differences"]) <= len(REVIEWABLE_DIFFERENCES),
            "invalid reviewed tuple differences")
    return value


def inventory(api, workspace, watched):
    raw = api.git(workspace, "ls-tree", "-r", "HEAD", "--", *watched)
    result = {}
    for line in raw.splitlines():
        left, path = line.split("\t")
        mode, kind, oid = left.split()
        require(path not in result and not PurePosixPath(path).is_absolute()
                and ".." not in PurePosixPath(path).parts, "invalid watched path")
        require((mode, kind) in {("100644", "blob"), ("100755", "blob"), ("160000", "commit")}
                and re.fullmatch(r"[0-9a-f]{40}", oid), "invalid watched tuple")
        if kind == "blob":
            file = workspace / path
            require(file.resolve(strict=True).is_relative_to(workspace.resolve()), "watched input escapes checkout")
            data = regular_bytes(file)
            require(hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == oid,
                    "materialized watched bytes differ from immutable source")
            require(bool(file.stat().st_mode & 0o111) == (mode == "100755"), "watched file mode differs")
        result[path] = {"mode": mode, "type": kind, "sha": oid}
    require(result, "empty watched inventory")
    return result, digest(raw.encode())


def compare_inventories(producer, consumer, reviewed):
    actual = []
    for path in sorted(producer.keys() | consumer.keys()):
        if producer.get(path) != consumer.get(path):
            require(path in REVIEWABLE_DIFFERENCES, "unapproved production input difference: " + path)
            actual.append({"path": path, "producer": producer.get(path), "consumer": consumer.get(path),
                           "reason": REVIEWABLE_DIFFERENCES[path]})
    require(actual == reviewed, "actual input differences do not match the separately pinned review")


def compare_production(producer, consumer, recorded_configuration):
    require(set(consumer["kernel_config"]) == SETTINGS, "all sixteen effective settings are required")
    for field in PRODUCTION_FIELDS:
        require(consumer[field] == producer[field], "kernel production identity differs: " + field)
    require(consumer["kernel_config"] == recorded_configuration, "effective configuration differs from original bundle")


def verify_catalog(api, root, context, source, provenance):
    require(api.git(root, "rev-parse", "HEAD") == context["catalog"]["commit"], "catalog commit differs")
    require(context["consumer"]["registry"] == api.REGISTRY_PREFIX + context["catalog"]["commit"]
            and api.selected_registry() == context["consumer"]["registry"], "consumer requires the explicit immutable catalog")
    for module, recorded in (("sonic-linux-kernel", "kernel_source_json_sha256"),
                             ("sonic-build-infra", "infra_source_json_sha256")):
        entry = root / "modules" / module / source["modules"][module]
        descriptor = regular_bytes(entry / "source.json", 256 * 1024)
        require(digest(descriptor) == provenance["resolution"][recorded], "catalog source descriptor differs from original")
        parsed = strict_json(descriptor)
        selected = [str((entry / "source.json").relative_to(root))]
        for group in ("patches", "overlay"):
            values = parsed.get(group, {})
            require(isinstance(values, dict), "unknown registry file map")
            for name, sri in values.items():
                relative = PurePosixPath(name)
                require(not relative.is_absolute() and ".." not in relative.parts and name,
                        "invalid registry file path")
                file = entry / group / name
                data = regular_bytes(file)
                require(sri == "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode(),
                        "registry patch or overlay differs from original SRI")
                selected.append(str(file.relative_to(root)))
        # Verify both immutable Git tuples and the actual materialized bytes.
        actual, _ = inventory(api, root, selected)
        require(set(actual) == set(selected), "registry input is absent from its pinned commit")


@contextmanager
def original_registry(value):
    old = os.environ.get("SONIC_REVIEW_REGISTRY")
    os.environ["SONIC_REVIEW_REGISTRY"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SONIC_REVIEW_REGISTRY", None)
        else:
            os.environ["SONIC_REVIEW_REGISTRY"] = old


def load_original(api, workspace, expected_sha256):
    path = workspace / "tools/bazel/kernel/prepare.py"
    data = regular_bytes(path, 256 * 1024)
    require(digest(data) == expected_sha256, "original verifier source differs")
    resolution = "tools/bazel/ci/resolution.py"
    require(regular_bytes(workspace / resolution) == regular_bytes(Path(api.resolution.__file__)),
            "original verifier import differs from the loaded reviewed resolution helper")
    spec = importlib.util.spec_from_file_location("_sonic_original_kernel_verifier", path)
    require(spec is not None and spec.loader is not None, "original verifier is not loadable")
    original = importlib.util.module_from_spec(spec)
    old_path = sys.path[:]
    old_bytecode = sys.dont_write_bytecode
    try:
        # Execute only the authenticated source bytes, never an existing pyc.
        sys.dont_write_bytecode = True
        exec(compile(data, str(path), "exec"), original.__dict__)
    finally:
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode
    return original


def verify(api, bundle, workspace, *, context_path, context_sha256, producer_workspace,
           registry_workspace, architecture, kernel_config):
    require(set(kernel_config) == SETTINGS, "producer-context verification requires every effective Make setting")
    context = load_context(context_path, context_sha256)
    workspace = workspace.resolve(strict=True)
    producer_workspace = producer_workspace.resolve(strict=True)
    registry_workspace = registry_workspace.resolve(strict=True)
    require(workspace != producer_workspace, "producer and consumer checkouts must remain distinct")
    require(Path(api.__file__).resolve() == workspace / "tools/bazel/kernel/prepare.py"
            and Path(__file__).resolve() == workspace / "tools/bazel/kernel/reuse_context.py",
            "execute the verifier from the explicitly selected consumer checkout")
    for name, root in (("producer", producer_workspace), ("consumer", workspace)):
        require(api.git(root, "rev-parse", "HEAD") == context[name]["commit"], name + " commit differs")
    bundle = bundle.resolve(strict=True)
    for filename, key in ((api.MANIFEST, "manifest_sha256"), (api.PROVENANCE, "provenance_sha256")):
        require(digest(regular_bytes(bundle / filename, 256 * 1024)) == context["producer"][key],
                "original metadata hash differs: " + filename)
    provenance = strict_json(regular_bytes(bundle / api.PROVENANCE, 256 * 1024))
    require(provenance["source_commit"] == context["producer"]["commit"]
            and provenance["resolution"]["registry"] == context["producer"]["registry"]
            and provenance["consumer_inputs_sha256"] == context["producer"]["inputs_sha256"],
            "original producer identity differs from the independently pinned context")
    producer_watched = list(api.WATCHED) + ["tools/bazel/debian_mirrors.cfg"]
    producer_inputs, producer_digest = inventory(api, producer_workspace, producer_watched)
    consumer_inputs, consumer_digest = inventory(api, workspace, api.WATCHED)
    require(producer_digest == context["producer"]["inputs_sha256"]
            and consumer_digest == context["consumer"]["inputs_sha256"], "watched source identity differs")
    compare_inventories(producer_inputs, consumer_inputs, context["differences"])
    original = load_original(api, producer_workspace, context["producer"]["prepare_sha256"])
    require(original.WATCHED == producer_watched, "unexpected original verifier watch contract")
    producer = original.source_state(producer_workspace, architecture, kernel_config)
    consumer = api.source_state(workspace, architecture, kernel_config)
    require(producer["consumer_inputs_sha256"] == producer_digest
            and consumer["consumer_inputs_sha256"] == consumer_digest, "source APIs differ from independently checked inventories")
    compare_production(producer, consumer, provenance["kernel_config"])
    require(api.contract(workspace, architecture, consumer["kernel_config"])
            == original.contract(producer_workspace, architecture, producer["kernel_config"]),
            "current package contract differs from the original producer")
    verify_catalog(api, registry_workspace, context, producer, provenance)
    with original_registry(context["producer"]["registry"]):
        # Existing five-package byte/control, manifest, provenance and registry
        # checks remain authoritative. No build/ensure/plan function is called.
        result = original.verify(bundle, producer_workspace, source=producer,
                                 architecture=architecture, kernel_config=kernel_config)
    for filename, key in ((api.MANIFEST, "manifest_sha256"), (api.PROVENANCE, "provenance_sha256")):
        require(digest(regular_bytes(bundle / filename, 256 * 1024)) == context["producer"][key],
                "original metadata changed during verification")
    return result
