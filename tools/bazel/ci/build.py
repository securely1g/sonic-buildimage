"""Build declared archive targets and locate their source repositories."""

from functools import partial
from pathlib import Path

from tools.bazel.build_helpers import export_archive
from tools.bazel.ci import artifact_validation, command_log


def collect_archives(workspace, directory, receipt, targets, *, bazel="bazel", options=()):
    """Build filename-to-label declarations, exporting one nonempty file each.

    The caller owns target selection and payload validation. These are archive
    artifacts: publication uses the shared helper's mode 0644 and keeps a prior
    output intact if a query or copy fails. Commands and completed exports remain
    in the caller's receipt on failure.
    """
    if not targets or any(not name or Path(name).name != name or name in (".", "..") for name in targets):
        raise ValueError("archive declarations need nonempty, plain output filenames")
    execute = partial(command_log.execute, cwd=workspace)
    receipt.setdefault("artifacts", {})
    execute([bazel, "build", *options,
             "--build_event_json_file=" + str(directory / "build-events.jsonl"),
             *targets.values()], directory, receipt, "build")
    paths = {}
    for name, target in targets.items():
        files = execute([bazel, "cquery", *options, "--output=files", target],
                        directory, receipt, name + ".query").splitlines()
        if len(files) != 1:
            raise ValueError("Expected exactly one nonempty archive output for " + target)
        source = workspace / files[0]
        if not source.is_file() or not source.stat().st_size:
            raise ValueError("Expected exactly one nonempty archive output for " + target)
        destination = directory / name
        export_archive(source, destination)
        paths[name] = destination
        receipt["artifacts"][name] = {"target": target, "bytes": destination.stat().st_size,
                                      "sha256": artifact_validation.sha(destination)}
    return paths


def source_directory(workspace, directory, receipt, target, *, bazel="bazel", options=(), name="source"):
    """Resolve an arbitrary target's repository, without a configured info query."""
    execute = partial(command_log.execute, cwd=workspace)
    # output_base needs no configured target; info execution_root can misresolve
    # root-module repository aliases in --platforms with Bazel 8.5.1.
    bases = execute([bazel, "info", "output_base"], directory, receipt,
                    name + ".output-base").splitlines()
    roots = execute([bazel, "cquery", *options, "--output=starlark",
                     "--starlark:expr=target.label.workspace_root", target],
                    directory, receipt, name + ".root").splitlines()
    if len(bases) != 1 or not bases[0] or len(roots) != 1:
        raise ValueError("Expected one source repository for " + target)
    source = (Path(bases[0]) / artifact_validation.path_name(roots[0])) if roots[0] else workspace
    if not source.is_dir():
        raise ValueError("Resolved source repository is missing for " + target + ": " + str(source))
    return source
