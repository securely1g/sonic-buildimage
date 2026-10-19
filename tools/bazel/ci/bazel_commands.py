"""Audit Bazel action graphs before package-producing execution."""
import hashlib
import json
import re

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
    outputs, suspicious, deb_actions = [], [], []
    for action in value.get("actions", []):
        outputs.extend(artifacts[identifier] for identifier in action.get("outputIds", []))
        arguments = action.get("arguments", [])
        command = " ".join(arguments)
        wrapper_text = " ".join(
            "\n".join(line for line in argument.splitlines() if not line.lstrip().startswith("#"))
            for argument in arguments)
        wrapper = re.search(r"(?:^|[/\s])(?:make|gmake|dpkg-buildpackage)(?:\s|$)", wrapper_text)
        deb_build = "dpkg-deb" in command and re.search(r"(?:--build|(?:^|\s)-b(?:\s|$))", command)
        if wrapper:
            suspicious.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
        if deb_build:
            deb_actions.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
    deb_outputs = sorted(path for path in outputs if path.endswith(".deb"))
    result = {"schema": 1, "action_count": len(value.get("actions", [])), "output_count": len(outputs),
              "deb_outputs": deb_outputs, "deb_packaging_actions": deb_actions, "packaging_wrappers": suspicious}
    return result


def audit_targets(bazel, options, targets, *, workspace, output, allow_deb=False):
    """Inspect package actions; allow DEBs only for an explicitly approved caller."""
    import subprocess
    import tempfile
    from pathlib import Path

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Action graphs may contain command environments; retain only this summary.
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as graph:
        subprocess.run([bazel, "aquery", *options, "--output=jsonproto",
                        "deps(set(" + " ".join(targets) + "))"],
                       cwd=workspace, stdout=graph, check=True)
        graph.flush()
        report = inspect_actions(Path(graph.name))
    report["targets"] = list(targets)
    report["deb_generation_authorized"] = allow_deb
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if ((report["deb_outputs"] or report["deb_packaging_actions"]) and not allow_deb) or report["packaging_wrappers"]:
        raise ValueError("DEB or packaging wrapper found in Bazel action graph")
    return report


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Audit targets before no-DEB Bazel execution")
    parser.add_argument("--bazel", default="bazel")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args()
    audit_targets(args.bazel, args.bazel_arg, args.targets,
                  workspace=Path.cwd(), output=args.output)


if __name__ == "__main__":
    main()
