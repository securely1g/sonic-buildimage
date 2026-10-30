"""Resolve verified outputs from the Build Event Protocol for one invocation."""

import json
import re
from pathlib import Path
import urllib.parse


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bep_outputs(path, targets, output_root):
    """Resolve successful target default outputs from this build's BEP only."""
    named, completed, finished, completion_seen = {}, {}, False, False
    with path.open() as stream:
        for line in stream:
            event = json.loads(line)
            identity = event.get("id", {})
            if "namedSet" in identity:
                named[identity["namedSet"]["id"]] = event["namedSetOfFiles"]
            if "targetCompleted" in identity:
                target = identity["targetCompleted"]
                if target.get("label") in targets and not target.get("aspect"):
                    require(target["label"] not in completed, "ambiguous target completion in BEP")
                    completed[target["label"]] = event["completed"]
            if "buildFinished" in identity:
                require(not completion_seen, "ambiguous build completion in BEP")
                completion_seen = True
                completion = event.get("finished")
                require(isinstance(completion, dict) and isinstance(completion.get("exitCode"), dict),
                        "BEP build completion lacks an explicit exitCode message")
                exit_code = completion["exitCode"]
                # Protobuf JSON omits scalar defaults, including successful code
                # zero. Require the enclosing message and overall success first
                # so a missing/failed completion cannot be mistaken for success.
                code = exit_code.get("code", 0)
                finished = (completion.get("overallSuccess") is True and type(code) is int and code == 0
                            and exit_code.get("name", "SUCCESS") == "SUCCESS")
    require(finished, "BEP does not report a successful completed build")

    def files(identifier, ancestors):
        require(identifier not in ancestors, "cyclic BEP named file sets")
        require(identifier in named, "missing BEP named file set")
        node = named[identifier]
        for file in node.get("files", []):
            uri = urllib.parse.urlparse(file.get("uri", ""))
            require(uri.scheme == "file" and not uri.netloc and not uri.query and not uri.fragment,
                    "BEP output must be a local file URI")
            output = Path(urllib.parse.unquote(uri.path)).resolve(strict=True)
            require(output.is_relative_to(output_root) and output.is_file(),
                    "BEP output escapes this invocation's Bazel output root")
            require(output.stat().st_size > 0, "empty Bazel output: " + str(output))
            yield output
        for child in node.get("fileSets", []):
            yield from files(child["id"], ancestors | {identifier})

    result = {}
    for target in targets:
        require(target in completed and completed[target].get("success"),
                "BEP target did not succeed: " + target)
        groups = [group for group in completed[target].get("outputGroup", [])
                  if group["name"] == "default"]
        require(len(groups) == 1 and not groups[0].get("incomplete"),
                "BEP lacks a complete default output group: " + target)
        outputs = {file for group in groups for node in group.get("fileSets", [])
                   for file in files(node["id"], set())}
        require(outputs, "BEP target has no output files: " + target)
        result[target] = outputs
    return result


def named_output(outputs, target, name):
    matches = [path for path in outputs[target] if path.name == name]
    require(len(matches) == 1, "missing or ambiguous Bazel output: " + name)
    return matches[0]


def output_identity(events, output):
    """Bind copied output bytes to the digest reported by this invocation."""
    identities = set()
    with events.open() as stream:
        for line in stream:
            for entry in json.loads(line).get("namedSetOfFiles", {}).get("files", []):
                uri = urllib.parse.urlparse(entry.get("uri", ""))
                if uri.scheme != "file" or Path(urllib.parse.unquote(uri.path)).resolve() != output:
                    continue
                digest, length = entry.get("digest", ""), entry.get("length", "")
                require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                        "BEP output lacks a SHA256 digest")
                require(str(length).isdigit() and int(length) > 0, "BEP output lacks a positive length")
                identities.add((digest, int(length)))
    require(len(identities) == 1, "missing or conflicting BEP output identity")
    digest, length = identities.pop()
    return {"sha256": digest, "bytes": length}
