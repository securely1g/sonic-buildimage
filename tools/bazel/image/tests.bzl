"""Rule contract tests with explicitly supplied execution-tool targets."""

load("@rules_python//python:defs.bzl", "py_test")

def image_assembly_test(name, tar_split = None, openssl = None, grub_editenv = None, zstd = None, **kwargs):
    """Supply real tools to enable Docker roundtrip, CMS and GRUB checks."""
    tools = {"tar-split": tar_split, "openssl": openssl, "grub-editenv": grub_editenv, "zstd": zstd}
    py_test(
        name = name,
        srcs = [Label(":assembly_test.py")],
        main = Label(":assembly_test.py"),
        deps = [Label(":assembly_lib")],
        args = ["--" + option + "=$(rootpath " + str(tool) + ")" for option, tool in tools.items() if tool] + kwargs.pop("args", []),
        data = [tool for tool in tools.values() if tool] + kwargs.pop("data", []),
        **kwargs
    )
