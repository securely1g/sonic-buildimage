"""Retain selected-source defaults and controller-owned CI policy."""

from pathlib import Path
import shlex
import sys


VALUE_OPTIONS = {
    "--disk_cache", "--repository_cache", "--distdir",
    "--downloader_config", "--experimental_downloader_config",
    "--remote_cache", "--experimental_remote_downloader",
    "--remote_upload_local_results",
}
FLAG_OPTIONS = {
    "--experimental_repository_cache_hardlinks",
    "--experimental_remote_downloader_local_fallback",
}


def configuration(workspace, controller):
    # Keep the selected source's checked-in build semantics. Its user RC and
    # ambient home RC cannot change the controller's download/cache policy.
    lines = (workspace / "tools/bazel/common.bazelrc").read_text().splitlines()
    local_import = "try-import %workspace%/.bazelrc.user"
    if lines.count(local_import) != 1:
        raise ValueError("common.bazelrc local import changed")
    lines.remove(local_import)
    # The controller combines this local default with its verified shared
    # mirrors. Use only the controller's absolute downloader path below.
    local_downloader = "common --downloader_config=tools/bazel/debian_mirrors.cfg"
    if lines.count(local_downloader) > 1:
        raise ValueError("common.bazelrc downloader default is duplicated")
    if local_downloader in lines:
        lines.remove(local_downloader)
    controlled = VALUE_OPTIONS | FLAG_OPTIONS | {
        "--noexperimental_repository_cache_hardlinks",
        "--noexperimental_remote_downloader_local_fallback",
        "--noremote_upload_local_results",
    }
    for line in lines:
        tokens = shlex.split(line, comments=True)
        if tokens == ["build", "--remote_upload_local_results=false"]:
            continue
        if tokens and (tokens[0] in ("import", "try-import", "startup") or
                       any(t.split("=", 1)[0] in controlled or
                           t == "--registry" or t.startswith("--registry=")
                           for t in tokens[1:])):
            raise ValueError("unexpected policy, startup, registry or import in common.bazelrc")
    policy = controller / ".bazelrc.user"
    if policy.is_symlink() or not policy.is_file():
        raise ValueError("controller policy RC is missing or redirected")
    seen = set()
    for line in policy.read_text().splitlines():
        tokens = shlex.split(line, comments=True)
        if not tokens:
            continue
        if len(tokens) != 2 or tokens[0] not in ("common", "build", "test"):
            raise ValueError("unexpected controller policy line")
        option, separator, value = tokens[1].partition("=")
        if option in VALUE_OPTIONS:
            if not separator or not value:
                raise ValueError("controller policy value is missing")
            if option == "--remote_upload_local_results" and value not in ("true", "false"):
                raise ValueError("controller upload policy must be true or false")
        elif option not in FLAG_OPTIONS or separator:
            raise ValueError("unexpected controller policy option")
        if (tokens[0], option) in seen:
            raise ValueError("duplicate controller policy option")
        seen.add((tokens[0], option))
        lines.append(tokens[0] + " " + shlex.quote(tokens[1]))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(configuration(Path.cwd(), Path(__file__).resolve().parents[3]))
