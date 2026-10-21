"""Reject DEB production before building source TARs and OCI images."""
import hashlib
import json
import re
import shlex

# Existing upstream compilation/install recipes in the selected image closure.
# This is not permission to package DEBs, or to wrap SONiC builds in Make.
FOREIGN_SOURCE_SCRIPTS = {
    "@@p4lang-pi++_repo_rules+pi_source//:sdk": "p4lang-pi++_repo_rules+pi_source/sdk_foreign_cc/build_script.sh",
    "@@p4lang-bmv2++_repo_rules+bmv2_source//:bmv2_build": "p4lang-bmv2++_repo_rules+bmv2_source/bmv2_build_foreign_cc/build_script.sh",
    "@@p4c++_repo_rules+p4c_source//:build": "p4c++_repo_rules+p4c_source/build_foreign_cc/build_script.sh",
    "@@sonic-fips+//openssl:build": "sonic-fips+/openssl/build_foreign_cc/build_script.sh",
    "@@sonic-fips+//openssh:build": "sonic-fips+/openssh/build_foreign_cc/build_script.sh",
    "@@sonic-fips+//krb5:build": "sonic-fips+/krb5/build_foreign_cc/build_script.sh",
    "@@sonic-fips+//symcrypt:core_": "sonic-fips+/symcrypt/symcrypt_foreign_cc/build_script.sh",
    "@@sonic-fips+//symcrypt:provider": "sonic-fips+/symcrypt/symcrypt_openssl_foreign_cc/build_script.sh",
    "@@socat++_repo_rules+socat_source//:socat_build": "socat++_repo_rules+socat_source/socat_build_foreign_cc/build_script.sh",
    "@@iproute2++_repo_rules+iproute2_source//:iproute2_build": "iproute2++_repo_rules+iproute2_source/iproute2_build_foreign_cc/build_script.sh",
}
MAKE = re.compile(r"(?:^|[/\s])(?:make|gmake)(?:\s|$)")
DECLARED_MAKE = re.compile(r"\$EXT_BUILD_ROOT/bazel-out/[^/]+/bin/external/sonic-build-infra\+/tools/build_tools/configure_runtime_(?:amd64|arm64)/bin/make")


def foreign_source_script(action, owner, outputs, lines):
    expected = FOREIGN_SOURCE_SCRIPTS.get(owner)
    if not (expected and action.get("mnemonic") == "FileWrite" and action.get("isExecutable") is True and
            len(outputs) == 1 and re.fullmatch(r"bazel-out/[^/]+/bin/external/" + re.escape(expected), outputs[0])):
        return False
    for line in lines:
        if not MAKE.search(line):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            return False
        if not words:
            return False
        if words[0] == "symlink_to_dir" and len(words) == 4 and DECLARED_MAKE.fullmatch(words[1]):
            continue
        if words[0].endswith("/cmake") and any(
                word.startswith("-DCMAKE_MAKE_PROGRAM=") and DECLARED_MAKE.fullmatch(word.split("=", 1)[1])
                for word in words):
            continue
        if not DECLARED_MAKE.fullmatch(words[0]):
            return False
        for word in words[1:]:
            if word in {"all", "build_sw", "install", "install_sw", "install_ssldirs", "install-nokeys"}:
                continue
            if (owner in {"@@p4lang-pi++_repo_rules+pi_source//:sdk",
                          "@@p4lang-bmv2++_repo_rules+bmv2_source//:bmv2_build"} and
                    "=" in word and word.split("=", 1)[0] in {"V", "pythondir", "pyexecdir"}):
                continue
            if re.fullmatch(r"-j[0-9]+", word) or ("=" in word and word.split("=", 1)[0] in {
                    "DESTDIR", "ASKPASS_PROGRAM", "COMPILE_ET", "MAKE_COMMANDS"}):
                continue
            return False
    return True

def invoked_command(arguments):
    """Inspect executable commands, without treating declared tool inputs as calls.

    Ordinary action arguments can name tools for a reviewed source generator.
    Shell bodies and standard command launchers still need the broader scan;
    declared outputs are checked independently for every action.
    """
    if not arguments:
        return ""
    program = arguments[0].rsplit("/", 1)[-1]
    if program in {"sh", "bash", "dash", "zsh", "ksh", "env", "timeout", "nice"}:
        return "\n".join(line for argument in arguments for line in argument.splitlines()
                         if not line.lstrip().startswith("#"))
    return arguments[0]


def inspect_actions(path):
    value = json.loads(path.read_bytes())
    fragments = {item["id"]: item for item in value.get("pathFragments", [])}
    memo = {}
    def fragment(identifier):
        if identifier not in memo:
            item = fragments[identifier]
            memo[identifier] = ((fragment(item["parentId"]) + "/") if item.get("parentId") else "") + item["label"]
        return memo[identifier]
    artifacts = {item["id"]: fragment(item["pathFragmentId"]) for item in value.get("artifacts", [])}
    owners = {item["id"]: item["label"] for item in value.get("targets", [])}
    outputs, suspicious, deb_actions, foreign = [], [], [], []
    for action in value.get("actions", []):
        action_outputs = [artifacts[identifier] for identifier in action.get("outputIds", [])]
        outputs.extend(action_outputs)
        # Packaging commands can be in a generated script rather than the
        # action's command line. Request and inspect FileWrite contents too.
        arguments = [*action.get("arguments", []), action.get("fileContents", "")]
        command = " ".join(arguments)
        lines = [line for argument in arguments for line in argument.splitlines()
                 if not line.lstrip().startswith("#")]
        wrapper_text = "\n".join(lines)
        packaging = re.search(r"(?:^|[/\s])dpkg-buildpackage(?:\s|$)", wrapper_text)
        wrapper = MAKE.search(invoked_command(action.get("arguments", []))) or MAKE.search("\n".join(
            line for line in action.get("fileContents", "").splitlines() if not line.lstrip().startswith("#"))) or packaging
        deb_build = "dpkg-deb" in command and re.search(r"(?:--build|(?:^|\s)-b(?:\s|$))", command)
        owner = owners.get(action.get("targetId"))
        if (wrapper and not packaging and not deb_build and
                not MAKE.search(" ".join(action.get("arguments", []))) and
                foreign_source_script(action, owner, action_outputs, lines)):
            foreign.append({"owner": owner, "script": action_outputs[0],
                            "sha256": hashlib.sha256(action["fileContents"].encode()).hexdigest()})
            wrapper = False
        if wrapper:
            suspicious.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
        if deb_build:
            deb_actions.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
    deb_outputs = sorted(path for path in outputs if path.endswith(".deb"))
    result = {"schema": 1, "action_count": len(value.get("actions", [])), "output_count": len(outputs),
              "deb_outputs": deb_outputs, "deb_packaging_actions": deb_actions, "packaging_wrappers": suspicious,
              "foreign_source_builds": foreign}
    return result


def audit_targets(bazel, options, targets, *, workspace, output, startup=()):
    """Inspect the selected actions before any TAR/OCI build or test executes."""
    import subprocess
    import tempfile
    from pathlib import Path

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Action graphs may contain command environments; retain only this summary.
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as graph:
        subprocess.run([bazel, *startup, "aquery", *options, "--include_file_write_contents", "--output=jsonproto",
                        "deps(set(" + " ".join(targets) + "))"],
                       cwd=workspace, stdout=graph, check=True)
        graph.flush()
        report = inspect_actions(Path(graph.name))
    report["targets"] = list(targets)
    report["deb_generation_authorized"] = False
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["deb_outputs"] or report["deb_packaging_actions"] or report["packaging_wrappers"]:
        raise ValueError("DEB or packaging wrapper found in Bazel action graph")
    return report


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Audit targets before no-DEB Bazel execution")
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-startup-arg", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    audit_targets(args.bazel, args.bazel_arg, args.targets,
                  workspace=Path.cwd(), output=args.output,
                  startup=args.bazel_startup_arg)


if __name__ == "__main__":
    main()
