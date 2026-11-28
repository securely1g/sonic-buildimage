"""Run native consumers with Common's declared transitive runtime payload."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

def common_runtime_test(
        name,
        binary,
        test_args = [],
        data = [],
        runtime = "@sonic_swss_common//tools/bazel/runtime:test_runtime",
        **kwargs):
    """Wrap a native test without relying on libraries installed on the host."""
    sh_test(
        name = name,
        srcs = ["@sonic_swss_common//tools/bazel/runtime:run_with_runtime.py"],
        args = [
            "$(rootpath @patchelf//:patchelf)",
            "$(rootpath " + runtime + ")",
            "-",
        ] + select({
            "@sonic_swss_common//tools/bazel:yang_modules_enabled": ["enabled"],
            "//conditions:default": ["disabled"],
        }) + ["$(rootpath " + binary + ")"] + test_args,
        data = [runtime, binary, "@patchelf//:patchelf"] + data,
        **kwargs
    )
