"""Retain build defaults, local caches and downloader configuration."""

from pathlib import Path
import shlex
import sys


def configuration(workspace, home):
    # Keep the checked-in build semantics, but explicitly select local cache
    # options instead of importing module/registry overrides from user rc files.
    lines = (workspace / 'tools/bazel/common.bazelrc').read_text().splitlines()
    local_import = 'try-import %workspace%/.bazelrc.user'
    if lines.count(local_import) != 1:
        raise ValueError('common.bazelrc local import changed')
    lines.remove(local_import)
    for line in lines:
        tokens = shlex.split(line, comments=True)
        if tokens and (tokens[0] in ('import', 'try-import') or
                       any(t == '--registry' or t.startswith('--registry=') for t in tokens)):
            raise ValueError('unexpected registry/import in common.bazelrc')
    cache_options = {'--disk_cache', '--repository_cache', '--distdir',
                     '--experimental_repository_cache_hardlinks',
                     '--experimental_downloader_config'}
    for path in (home / '.bazelrc', workspace / '.bazelrc.user'):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens or tokens[0] not in ('common', 'build', 'test'):
                continue
            for index, token in enumerate(tokens[1:], 1):
                option = token.split('=', 1)[0]
                if option not in cache_options:
                    continue
                if '=' in token or option == '--experimental_repository_cache_hardlinks':
                    value = token
                else:
                    if index + 1 >= len(tokens):
                        raise ValueError('missing cache option value')
                    value = option + '=' + tokens[index + 1]
                lines.append(tokens[0] + ' ' + shlex.quote(value))
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    Path(sys.argv[1]).write_text(configuration(Path.cwd(), Path.home()))
