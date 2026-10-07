"""Run Bazel commands and audit action graphs before package-producing execution."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from tools.bazel.ci.artifact_validation import require

def run(arguments, log_path, *, workspace, output_path=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output = output_path.open("w") if output_path else subprocess.PIPE
    try:
        with log_path.open("w") as log:
            log.write("$ " + shlex.join(arguments) + "\n")
            process = subprocess.Popen(arguments, cwd=workspace, stdout=output,
                                       stderr=subprocess.PIPE if output_path else subprocess.STDOUT,
                                       text=True, bufsize=1)
            stream = process.stderr if output_path else process.stdout
            for line in stream:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            result = process.wait()
            require(result == 0, "command failed with exit " + str(result) + ": " + str(log_path))
    finally:
        if output_path:
            output.close()


def capture(arguments, log_path, *, workspace):
    output_path = log_path.with_suffix(log_path.suffix + ".stdout")
    run(arguments, log_path, workspace=workspace, output_path=output_path)
    return output_path.read_text().strip()


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
    outputs, suspicious = [], []
    for action in value.get("actions", []):
        outputs.extend(artifacts[identifier] for identifier in action.get("outputIds", []))
        command = " ".join(action.get("arguments", []))
        wrapper = re.search(r"(?:^|[/\s])(?:make|gmake|dpkg-buildpackage)(?:\s|$)", command)
        deb_build = "dpkg-deb" in command and re.search(r"(?:--build|(?:^|\s)-b(?:\s|$))", command)
        if wrapper or deb_build:
            suspicious.append({"mnemonic": action.get("mnemonic"), "arguments_sha256": hashlib.sha256(command.encode()).hexdigest()})
    deb_outputs = sorted(path for path in outputs if path.endswith(".deb"))
    result = {"schema": 1, "action_count": len(value.get("actions", [])), "output_count": len(outputs),
              "deb_outputs": deb_outputs, "packaging_wrappers": suspicious}
    return result
