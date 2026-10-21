"""Reject Debian package actions before CI builds or runs tests."""

import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import bazel_commands


class ActionAuditTest(unittest.TestCase):
    def audit(self, output_name, arguments, *, file_contents="", owner="//:checked", executable=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = {
                "actions": [{"outputIds": [1], "arguments": arguments, "fileContents": file_contents,
                             "targetId": 1, "mnemonic": "FileWrite", "isExecutable": executable,
                             "environmentVariables": [{"key": "PRIVATE", "value": "private-value"}]}],
                "targets": [{"id": 1, "label": owner}],
                "artifacts": [{"id": 1, "pathFragmentId": 1}],
                "pathFragments": [{"id": 1, "label": output_name}],
            }
            def query(command, *, cwd, stdout, check):
                self.assertEqual(command[1], "aquery")
                self.assertIn("--platforms=native", command)
                self.assertIn("--include_file_write_contents", command)
                stdout.write(json.dumps(graph))
                stdout.flush()
            with mock.patch("subprocess.run", side_effect=query):
                try:
                    return bazel_commands.audit_targets(
                        "bazel", ["--platforms=native"], ["//:checked"],
                        workspace=root, output=root / "audit.json")
                finally:
                    summary = (root / "audit.json").read_text()
                    self.assertNotIn("private-value", summary)

    def test_accepts_tar_assembly_and_debian_extraction(self):
        result = self.audit("runtime.tar", ["dpkg-deb", "--fsys-tarfile", "input.deb"])
        self.assertEqual(result["targets"], ["//:checked"])
        self.assertEqual(result["action_count"], 1)

    def test_rejects_debian_output(self):
        with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
            self.audit("package.deb", ["packager"])

    def test_accepts_declared_make_tool_for_source_generation(self):
        # A source generator receives a declared tool path as data. It is not
        # an action invoking an unreviewed Make packaging target directly.
        result = self.audit("generated_sources", [
            "bazel-out/bin/external/component/bazel/prepare", "prepare",
            "--make", "bazel-out/bin/external/infra/tools/build_tools/make",
            "--output", "bazel-out/bin/generated_sources",
        ])
        self.assertEqual(result["packaging_wrappers"], [])
    def test_shell_comment_is_not_a_make_invocation(self):
        result = self.audit("runtime.tar", ["bash", "-c", "# make sure data is compressed\ntar -czf data.tar.gz tree"])
        self.assertEqual(result["packaging_wrappers"], [])
        self.assertFalse(result["deb_generation_authorized"])
        with self.assertRaises(ValueError):
            self.audit("runtime.tar", ["bash", "-c", "# comment\nmake package"])

    def test_rejects_packaging_commands_in_generated_scripts(self):
        for command in ("dpkg-deb --build tree output", "make package", "dpkg-buildpackage -b"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                self.audit("package-script.sh", [], file_contents="#!/bin/sh\n" + command + "\n")

    def test_rejects_wrapped_packaging_with_hidden_output(self):
        for command in (["dpkg-deb", "--build", "tree", "output"],
                        ["sh", "-c", "make package"], ["dpkg-buildpackage", "-b"],
                        ["/declared/tools/make", "package"],
                        ["/usr/bin/env", "LC_ALL=C", "/declared/tools/make", "package"],
                        ["bash", "-ec", "/declared/tools/dpkg-buildpackage -b"],
                        ["package-helper", "--builder", "/usr/bin/dpkg-buildpackage"],
                        ["timeout", "30", "gmake", "package"]):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit("output", command)

    def foreign(self, command, *, owner="@@sonic-fips+//openssl:build", arguments=(), executable=True):
        return self.audit("bazel-out/k8-fastbuild/bin/external/sonic-fips+/openssl/build_foreign_cc/build_script.sh",
                          list(arguments), file_contents="#!/bin/sh\n" + command + "\n",
                          owner=owner, executable=executable)

    def test_records_reviewed_foreign_source_compilation_separately(self):
        make = "$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/tools/build_tools/configure_runtime_amd64/bin/make"
        result = self.foreign(make + " build_sw -j4 DESTDIR=$BUILD_TMPDIR/$INSTALL_PREFIX\n" +
                              make + " install_sw -j4 DESTDIR=$BUILD_TMPDIR/$INSTALL_PREFIX")
        self.assertEqual(result["packaging_wrappers"], [])
        self.assertEqual(result["foreign_source_builds"][0]["owner"], "@@sonic-fips+//openssl:build")
        self.assertEqual(len(result["foreign_source_builds"][0]["sha256"]), 64)

    def test_source_p4_recipes_keep_declared_make_and_reject_packaging(self):
        make = "$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/tools/build_tools/configure_runtime_amd64/bin/make"
        for owner, script in bazel_commands.FOREIGN_SOURCE_SCRIPTS.items():
            if not owner.startswith(("@@p4lang-", "@@p4c+")):
                continue
            output = "bazel-out/k8-fastbuild/bin/external/" + script
            recipe = make + " -j4 install DESTDIR=$BUILD_TMPDIR/$INSTALL_PREFIX"
            if owner.startswith("@@p4lang-"):
                recipe += " V=0 pythondir=/usr/lib/python3/dist-packages"
            with self.subTest(owner=owner):
                result = self.audit(output, [], file_contents=recipe, owner=owner, executable=True)
                self.assertEqual(result["foreign_source_builds"][0]["owner"], owner)
                for command in (recipe + " binary", recipe + "\ndpkg-deb --build tree output", "make install"):
                    with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                        self.audit(output, [], file_contents=command, owner=owner, executable=True)

    def test_foreign_script_requires_reviewed_owner_and_executable_output(self):
        make = "$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/tools/build_tools/configure_runtime_amd64/bin/make all"
        for kwargs in ({"owner": "@@unreviewed+//:build"}, {"executable": False}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                self.foreign(make, **kwargs)

    def test_p4c_uses_its_actual_foreign_output_directory(self):
        owner = "@@p4c++_repo_rules+p4c_source//:build"
        output = "bazel-out/k8-fastbuild/bin/external/p4c++_repo_rules+p4c_source/p4c_foreign_cc/build_script.sh"
        for architecture in ("amd64", "arm64"):
            make = ("$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/"
                    "tools/build_tools/configure_runtime_" + architecture + "/bin/make")
            recipe = "symlink_to_dir " + make + " $EXT_BUILD_DEPS/bin/ False\n"
            recipe += "$EXT_BUILD_ROOT/external/cmake/bin/cmake -DCMAKE_MAKE_PROGRAM=" + make
            with self.subTest(architecture=architecture):
                self.assertEqual(self.audit(output, [], file_contents=recipe, owner=owner,
                                            executable=True)["packaging_wrappers"], [])
                with self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit(output.replace("/p4c_foreign_cc/", "/build_foreign_cc/"), [],
                               file_contents=recipe, owner=owner, executable=True)

    def test_dash_declared_compiler_environment_and_install_prefix(self):
        owner = "@@sonic-dash+//dash-pipeline/SAI:dash_build"
        output = "bazel-out/k8-fastbuild/bin/external/sonic-dash+/dash-pipeline/SAI/dash_build_foreign_cc/build_script.sh"
        for architecture in ("amd64", "arm64"):
            make = ("$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/"
                    "tools/build_tools/configure_runtime_" + architecture + "/bin/make")
            environment = ('ARFLAGS="" AR_FLAGS="" ASFLAGS="" CFLAGS="-g -O2" '
                           'CXXFLAGS="-g -O2" LDFLAGS="-L$EXT_BUILD_DEPS/sdk/lib" '
                           'AR="$EXT_BUILD_ROOT/bin/ar" CC="$EXT_BUILD_ROOT/bin/gcc" '
                           'CXX="$EXT_BUILD_ROOT/bin/g++" LD="$EXT_BUILD_ROOT/bin/ld" '
                           'RANLIB="" CPPFLAGS="-I$EXT_BUILD_DEPS/sdk/include" ')
            recipe = "symlink_to_dir " + make + " $EXT_BUILD_DEPS/bin/ False\n"
            recipe += "\n".join(environment + make + " " + target + " -j4 PREFIX=$INSTALLDIR"
                                for target in ("all", "install"))
            with self.subTest(architecture=architecture):
                self.assertEqual(self.audit(output, [], file_contents=recipe, owner=owner,
                                            executable=True)["foreign_source_builds"][0]["owner"], owner)
            for bad in (recipe.replace(" all ", " package "),
                        recipe.replace("PREFIX=$INSTALLDIR", "PREFIX=/unreviewed"),
                        recipe.replace('CFLAGS="-g -O2"', 'MAKEFLAGS="-f packaging.mk"'),
                        recipe.replace('CFLAGS="-g -O2"', 'CFLAGS="$(touch bad)"'),
                        recipe.replace('CFLAGS="-g -O2"', 'CFLAGS="`touch bad`"'),
                        recipe + "\ndpkg-deb --build tree output", recipe + "\ndpkg-buildpackage -b",
                        "make install PREFIX=$INSTALLDIR"):
                with self.subTest(architecture=architecture, recipe=bad), self.assertRaises(ValueError):
                    self.audit(output, [], file_contents=bad, owner=owner, executable=True)
            for bad_owner, bad_output in (("@@unreviewed+//:dash_build", output),
                                          (owner, output.replace("/dash_build_foreign_cc/", "/other/"))):
                with self.assertRaises(ValueError):
                    self.audit(bad_output, [], file_contents=recipe, owner=bad_owner, executable=True)

    def test_foreign_owner_does_not_allow_direct_make_or_other_make_tools(self):
        for command, arguments in (("make all", ()), ("echo test", ("make", "all"))):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                self.foreign(command, arguments=arguments)

    def test_foreign_owner_never_authorizes_debian_packaging(self):
        make = "$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/tools/build_tools/configure_runtime_amd64/bin/make"
        for command in (make + " package", make + " binary", make + " all\ndpkg-deb --build tree output",
                        make + " all\ndpkg-buildpackage -b"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                self.foreign(command)

    def test_nested_iproute2_source_requires_exact_owner_path_and_native_recipe(self):
        owner = "@@iproute2++_repo_rules+iproute2_source//:iproute2_build"
        script = "bazel-out/k8-fastbuild-ST-7e42167d27dc/bin/external/iproute2++_repo_rules+iproute2_source/iproute2_build_foreign_cc/build_script.sh"
        for architecture in ("amd64", "arm64"):
            make = ("$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/"
                    "tools/build_tools/configure_runtime_" + architecture + "/bin/make")
            recipe = ("symlink_to_dir " + make + " $EXT_BUILD_DEPS/bin/ False\n" +
                      make + " -j4\n" + make + " install DESTDIR=$BUILD_TMPDIR/$INSTALL_PREFIX -j4\n")
            with self.subTest(architecture=architecture):
                result = self.audit(script, [], file_contents=recipe, owner=owner, executable=True)
                self.assertEqual(result["packaging_wrappers"], [])
                self.assertEqual(result["foreign_source_builds"][0]["owner"], owner)
            for bad_owner, bad_script, bad_recipe in (
                    ("@@iproute2+//:iproute2_build", script, recipe),
                    (owner.replace("iproute2_source", "unreviewed_source"), script, recipe),
                    (owner, script.replace("iproute2_source", "unreviewed_source"), recipe),
                    (owner, script, recipe + make + " package\n"),
                    (owner, script, recipe + "dpkg-deb --build tree output\n"),
                    (owner, script, recipe + "dpkg-buildpackage -b\n")):
                with self.subTest(architecture=architecture, owner=bad_owner, script=bad_script,
                                  recipe=bad_recipe), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit(bad_script, [], file_contents=bad_recipe, owner=bad_owner, executable=True)

    def test_nested_socat_source_requires_exact_owner_path_and_native_recipe(self):
        owner = "@@socat++_repo_rules+socat_source//:socat_build"
        script = "bazel-out/k8-fastbuild-ST-7e42167d27dc/bin/external/socat++_repo_rules+socat_source/socat_build_foreign_cc/build_script.sh"
        for architecture in ("amd64", "arm64"):
            make = ("$EXT_BUILD_ROOT/bazel-out/k8-opt-exec/bin/external/sonic-build-infra+/"
                    "tools/build_tools/configure_runtime_" + architecture + "/bin/make")
            recipe = ("symlink_to_dir " + make + " $EXT_BUILD_DEPS/bin/ False\n" +
                      make + " -j4\n" + make + " install DESTDIR=$BUILD_TMPDIR/$INSTALL_PREFIX -j4\n")
            with self.subTest(architecture=architecture):
                result = self.audit(script, [], file_contents=recipe, owner=owner, executable=True)
                self.assertEqual(result["packaging_wrappers"], [])
                self.assertEqual(result["foreign_source_builds"][0]["owner"], owner)
            for bad_owner, bad_script, bad_recipe in (
                    ("@@socat+//:socat_build", script, recipe),
                    (owner.replace("socat_source", "unreviewed_source"), script, recipe),
                    (owner, script.replace("socat_source", "unreviewed_source"), recipe),
                    (owner, script, recipe + make + " package\n"),
                    (owner, script, recipe + "dpkg-deb --build tree output\n"),
                    (owner, script, recipe + "dpkg-buildpackage -b\n")):
                with self.subTest(architecture=architecture, owner=bad_owner, script=bad_script,
                                  recipe=bad_recipe), self.assertRaisesRegex(ValueError, "DEB or packaging wrapper"):
                    self.audit(bad_script, [], file_contents=bad_recipe, owner=bad_owner, executable=True)


if __name__ == "__main__":
    unittest.main()
