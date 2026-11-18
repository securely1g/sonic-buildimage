"""Check batched configured archives against individual builds with real Bazel."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.cache_reuse import json_records


CASES = [
    ("base.gz", [], "default VS normal.sai normal.sai"),
    ("versioned.gz", ["--//:version=v16"], "v16 VS normal.sai normal.sai"),
    ("syncd.gz", ["--config=syncd-dash"], "default DASH dash.sai dash.meta"),
    ("syncd-dbg.gz", ["--config=syncd-dash", "--//:version=v16"], "v16 DASH dash.sai dash.meta"),
]


def module(workspace, name, files):
    directory = workspace / name
    directory.mkdir()
    (directory / "MODULE.bazel").write_text(f'module(name="{name}", version="1.0")\n')
    for path, contents in files.items():
        dest = directory / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents)
    return f'bazel_dep(name="{name}", version="1.0")\nlocal_path_override(module_name="{name}", path="{name}")\n'


def fixture(workspace):
    # The real Syncd profile selects a boolean and two label flags in external
    # modules. Tiny inputs let CI verify the configuration without compiling DASH.
    flags = """
def _flag(ctx):
    return []

text = rule(implementation = _flag, build_setting = config.string(flag = True))
boolean = rule(implementation = _flag, build_setting = config.bool(flag = True))
"""
    public = 'package(default_visibility = ["//visibility:public"])\n'
    dependencies = module(workspace, "sonic_build_infra", {
        "platforms/BUILD": public + 'platform(name="x86_64_trixie")\nplatform(name="aarch64_trixie")\n',
    })
    dependencies += module(workspace, "sonic_dash", {
        "BUILD": public + 'filegroup(name="sdk", srcs=["dash.sai"])\n'
                          'filegroup(name="runtime_packages.json", srcs=["dash.meta"])\n',
        "dash.sai": "sdk", "dash.meta": "metadata",
    })
    dependencies += module(workspace, "sonic_sairedis", {
        "flags.bzl": flags,
        "BUILD": 'load(":flags.bzl", "boolean")\n' + public
                 + 'boolean(name="enable_dashsai", build_setting_default=False)\n'
                   'filegroup(name="standard", srcs=["normal.sai"])\n'
                   'label_flag(name="dash_sai", build_setting_default=":standard")\n'
                   'label_flag(name="dash_runtime_metadata", build_setting_default=":standard")\n',
        "normal.sai": "vs",
    })
    (workspace / "MODULE.bazel").write_text('module(name="batch_test")\n' + dependencies)
    (workspace / "flags.bzl").write_text(flags)
    (workspace / "probe.bzl").write_text("""
def _shared_child(ctx):
    out = ctx.actions.declare_file("ordinary-package.txt")
    ctx.actions.run(
        executable = "/bin/sh",
        arguments = ["-c", 'printf package > "$1"', "package", out.path],
        outputs = [out],
        mnemonic = "OrdinaryPackage",
    )
    return [DefaultInfo(files = depset([out]))]

shared_child = rule(implementation = _shared_child)

def _emit(ctx):
    out = ctx.actions.declare_file("probe.txt")
    content = " ".join([ctx.attr.version, ctx.attr.mode, ctx.file.sdk.basename, ctx.file.meta.basename])
    ctx.actions.run(
        executable = "/bin/sh",
        arguments = ["-c", 'test -s "$1" && printf %s "$2" > "$3"', "archive", ctx.file.package.path, content, out.path],
        inputs = [ctx.file.package],
        outputs = [out],
        mnemonic = "VersionedArchive",
    )
    return [DefaultInfo(files = depset([out]))]

probe = rule(implementation = _emit, attrs = {
    "package": attr.label(default = ":ordinary", allow_single_file = True),
    "version": attr.string(),
    "mode": attr.string(),
    "sdk": attr.label(default = "@sonic_sairedis//:dash_sai", allow_single_file = True),
    "meta": attr.label(default = "@sonic_sairedis//:dash_runtime_metadata", allow_single_file = True),
})
""")
    (workspace / "BUILD.bazel").write_text('''
load(":flags.bzl", "text")
load(":probe.bzl", "probe", "shared_child")
package(default_visibility = ["//visibility:public"])
text(name = "version", build_setting_default = "default")
shared_child(name = "ordinary")
config_setting(name = "v16", flag_values = {":version": "v16"})
config_setting(name = "v17", flag_values = {":version": "v17"})
config_setting(name = "dash", flag_values = {"@sonic_sairedis//:enable_dashsai": "true"})
probe(
    name = "archive",
    version = select({":v16": "v16", ":v17": "v17", "//conditions:default": "default"}),
    mode = select({":dash": "DASH", "//conditions:default": "VS"}),
)
''')
    tools = workspace / "tools/bazel"
    tools.mkdir(parents=True)
    (tools / "BUILD.bazel").touch()
    for filename in ("container_batch.bzl", "common.bazelrc"):
        shutil.copy(ROOT / "tools/bazel" / filename, tools)
    shutil.copy(ROOT / ".bazelversion", workspace)
    profile = [line for line in (tools / "common.bazelrc").read_text().splitlines()
               if line.startswith("build:syncd-dash ")]
    (workspace / ".bazelrc").write_text("\n".join(profile) + "\n")


def check_shared_cache(workspace, bazel, command, output):
    # A clean between builds discards local action state and outputs. Only the
    # execution cache can supply the version-independent package to v17.
    for version in ("v16", "v17"):
        if version == "v17":
            subprocess.run([bazel, "clean"], cwd=workspace, check=True)
        setting = "--//:version=" + version
        log = workspace / (version + ".json")
        options = ["--remote_cache=", "--remote_upload_local_results", "--disk_cache=" + str(workspace / "cache"),
                   "--execution_log_json_file=" + str(log)]
        env = dict(os.environ, BAZEL_CONTAINER_ARGS=shlex.join(options))
        subprocess.run([*command, "--archive=shared.gz", "--label=//:archive",
                        "--owner-args=" + setting, "--shared-args=" + setting], cwd=workspace, env=env, check=True)
        if (output / "shared.gz").read_text() != version + " VS normal.sai normal.sai":
            raise AssertionError(f"Shared version {version} did not reach the archive")
        records = list(json_records(log))
        for mnemonic in ("OrdinaryPackage", "VersionedArchive"):
            runners = [record["runner"] for record in records if record["mnemonic"] == mnemonic]
            want_hit = version == "v17" and mnemonic == "OrdinaryPackage"
            if len(runners) != 1 or (runners == ["disk cache hit"]) != want_hit:
                raise AssertionError(f"{version} {mnemonic}: expected disk cache hit={want_hit}, got {runners}")


def check(workspace, architecture):
    bazel = os.environ.get("BAZEL", "bazel")
    platform = "@sonic_build_infra//platforms:" + {"amd64": "x86_64_trixie", "arm64": "aarch64_trixie"}[architecture]
    output = workspace / "target/exports"
    command = [sys.executable, str(ROOT / "tools/bazel/build_container_batch.py"),
               "--architecture", architecture, "--target-path", str(output)]
    env = dict(os.environ, BAZEL_CONTAINER_ARGS="--remote_download_minimal")
    try:
        check_shared_cache(workspace, bazel, command, output)
        for name, flags, _ in CASES:
            command.extend(["--archive=" + name, "--label=//:archive", "--owner-args=" + " ".join(flags)])
        subprocess.run(command, cwd=workspace, env=env, check=True)
        mtimes = {name: (output / name).stat().st_mtime_ns for name, _, _ in CASES}
        subprocess.run(command, cwd=workspace, env=env, check=True)
        for name, flags, expected in CASES:
            direct = [bazel, "build", "//:archive", "--platforms=" + platform, "--host_platform=" + platform, *flags]
            subprocess.run(direct, cwd=workspace, check=True)
            exported = (output / name).read_bytes()
            if exported != (workspace / "bazel-bin/probe.txt").read_bytes() or exported != expected.encode():
                raise AssertionError(f"{name} differs from standalone build or expected configuration: {exported!r}")
            if (output / name).stat().st_mtime_ns != mtimes[name]:
                raise AssertionError(f"Rebuilding the unchanged archive modified {name}")
    finally:
        subprocess.run([bazel, "shutdown"], cwd=workspace, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sonic-container-batch-test-") as directory:
        workspace = Path(directory)
        fixture(workspace)
        check(workspace, args.architecture)
