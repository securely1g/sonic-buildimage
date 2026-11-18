"""Check batched configured archives against individual builds with real Bazel."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
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
def _emit(ctx):
    out = ctx.actions.declare_file("probe.txt")
    content = " ".join([ctx.attr.version, ctx.attr.mode, ctx.file.sdk.basename, ctx.file.meta.basename])
    ctx.actions.write(out, content)
    return [DefaultInfo(files = depset([out]))]

probe = rule(implementation = _emit, attrs = {
    "version": attr.string(),
    "mode": attr.string(),
    "sdk": attr.label(default = "@sonic_sairedis//:dash_sai", allow_single_file = True),
    "meta": attr.label(default = "@sonic_sairedis//:dash_runtime_metadata", allow_single_file = True),
})
""")
    (workspace / "BUILD.bazel").write_text('''
load(":flags.bzl", "text")
load(":probe.bzl", "probe")
package(default_visibility = ["//visibility:public"])
text(name = "version", build_setting_default = "default")
config_setting(name = "versioned", flag_values = {":version": "v16"})
config_setting(name = "dash", flag_values = {"@sonic_sairedis//:enable_dashsai": "true"})
probe(
    name = "archive",
    version = select({":versioned": "v16", "//conditions:default": "default"}),
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


def check(workspace, architecture):
    bazel = os.environ.get("BAZEL", "bazel")
    platform = "@sonic_build_infra//platforms:" + {"amd64": "x86_64_trixie", "arm64": "aarch64_trixie"}[architecture]
    output = workspace / "target/exports"
    command = [sys.executable, str(ROOT / "tools/bazel/build_container_batch.py"),
               "--architecture", architecture, "--target-path", str(output)]
    for name, flags, _ in CASES:
        command.extend(["--archive=" + name, "--label=//:archive", "--owner-args=" + " ".join(flags)])
    env = dict(os.environ, BAZEL_CONTAINER_ARGS="--remote_download_minimal")
    try:
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
