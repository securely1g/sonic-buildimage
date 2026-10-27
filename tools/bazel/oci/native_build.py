#!/usr/bin/env python3
"""Run image Bazel commands in the declared native installation environment.

Make's ordinary build user keeps ownership of its checkout. A dedicated root
container can preserve Debian numeric ownership, while apt_install executes
package scripts in a second, private filesystem/network namespace.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

ENVIRONMENT = 'debian:trixie-20260918@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c'
COMMANDS = {'build', 'test', 'run', 'aquery', 'cquery', 'query', 'info', 'mod'}
NATIVE_SHARED_CACHE = Path('/bazel_cache/native-root')


def cache_arguments(arguments, shared_cache):
    """Keep root's writable caches separate from the ordinary Make user's.

    Command-line defaults override the mounted slave RC. Caller options follow
    these defaults, so an explicitly selected (or disabled) cache still wins.
    The shared source distdir remains available without changing its contents.
    """
    if not shared_cache:
        return list(arguments)
    return [arguments[0],
            '--repository_cache=' + str(NATIVE_SHARED_CACHE / 'repository_cache'),
            '--disk_cache=' + str(NATIVE_SHARED_CACHE / 'disk_cache'),
            *arguments[1:]]


@contextmanager
def output_lock(cache, startup):
    """Serialize containers sharing Bazel output state across PID namespaces.

    Each Docker container sees different server PIDs, so Bazel's own client
    coordination cannot protect a shared output tree. Keep this host lock
    beside that tree, where Bazel cleanup cannot remove it.
    """
    output_root = cache / 'bazel'
    output_base = None
    for argument in startup:
        if argument.startswith('--output_user_root='):
            output_root = Path(argument.split('=', 1)[1])
        elif argument.startswith('--output_base='):
            output_base = Path(argument.split('=', 1)[1])
    output = (output_base if output_base is not None else output_root).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.parent / (output.name + '.native-build.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def audit_command(argv, workspace):
    """Audit the exact Make-selected image graph inside its build environment."""
    from tools.bazel.ci import bazel_commands

    operation = next((index for index, value in enumerate(argv[1:], 1) if value in COMMANDS), None)
    if operation is None:
        raise ValueError('Missing Bazel image command')
    if argv[operation] not in {'build', 'test', 'run'}:
        return
    options, targets = [], []
    for argument in argv[operation + 1:]:
        if argument == '--':
            break  # Remaining arguments belong to the selected program.
        if argument.startswith('--'):
            options.append(argument)
        elif argument.startswith(('//', '@', ':')):
            targets.append(argument)
        else:
            raise ValueError('Native image builds require explicit Bazel target labels and --option=value syntax: ' + argument)
    if not targets:
        raise ValueError('Native image builds require explicit Bazel target labels')
    key = hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:16]
    bazel_commands.audit_targets(argv[0], options, targets, startup=argv[1:operation],
        workspace=workspace, output=workspace / 'target/bazel-native' / ('action-audit-' + key + '.json'))


def command(arguments, workspace, bazel, cache, startup=()):
    """Build Docker arguments as separate values; never evaluate shell options."""
    mounts = {workspace.resolve(): False, bazel.resolve(): True, cache.resolve(): False}
    git_directory = subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                            cwd=workspace, text=True).strip()
    git_path = Path(git_directory).resolve()
    if not git_path.is_relative_to(workspace.resolve()):
        mounts[git_path] = True
    # The standard slave cache and explicit cache/distdir inputs remain usable
    # at the same absolute paths inside and outside this container.
    shared_cache = Path('/bazel_cache').is_dir()
    paths = [Path('/bazel_cache')] if shared_cache else []
    for argument in [*startup, *arguments]:
        if argument.startswith(('--disk_cache=', '--repository_cache=', '--distdir=', '--output_base=', '--output_user_root=')):
            value = Path(argument.split('=', 1)[1])
            if value.is_absolute():
                paths.append(value)
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        if not any(path.resolve().is_relative_to(parent) for parent in mounts):
            mounts[path.resolve()] = False
    # The ordinary ignored RC is also used by reviewed draft-source preparation.
    # Expose those declared module inputs read-only when they live outside the
    # checkout; parsing options never executes their contents.
    source_arguments = list(arguments)
    user_rc = workspace / '.bazelrc.user'
    if user_rc.is_file():
        for line in user_rc.read_text().splitlines():
            source_arguments.extend(shlex.split(line, comments=True)[1:])
    for argument in source_arguments:
        if argument.startswith(('--override_module=', '--override_repository=')):
            fields = argument.split('=', 2)
            if len(fields) != 3:
                raise ValueError('Invalid Bazel source override: ' + argument)
            path = Path(fields[2])
            path = path if path.is_absolute() else workspace / path
            if not path.is_dir():
                raise ValueError('Bazel source override is not a directory: ' + str(path))
            if not path.resolve().is_relative_to(workspace.resolve()):
                mounts[path.absolute()] = True
                mounts[path.resolve()] = True
    result = ['docker', 'run', '--rm', '--init', '--cap-add=SYS_ADMIN', '--cap-add=NET_ADMIN',
              '--security-opt=seccomp=unconfined', '--security-opt=apparmor=unconfined',
              '--workdir', str(workspace.resolve()), '--env', 'APT_INSTALL_BUILD_CONTAINER=' + ENVIRONMENT,
              '--env', 'BAZELISK_HOME=' + str(NATIVE_SHARED_CACHE / 'bazelisk' if shared_cache else cache / 'bazelisk')]
    for path, readonly in mounts.items():
        if ',' in str(path):
            raise ValueError('Docker bind paths cannot contain commas: ' + str(path))
        result.extend(['--mount', 'type=bind,src=' + str(path) + ',dst=' + str(path) + (',readonly' if readonly else '')])
    if shared_cache:
        # Keep the ordinary slave configuration, but override its writable
        # cache defaults below so root does not populate the Make user's
        # caches. No existing shared cache entries need to be changed.
        slave_rc = workspace.resolve() / 'tools/bazel/slave.bazelrc'
        if not slave_rc.is_file():
            raise ValueError('Missing native build cache configuration: ' + str(slave_rc))
        result.extend(['--mount', 'type=bind,src=' + str(slave_rc) + ',dst=/etc/bazel.bazelrc,readonly'])
    script = '''set -eu
apt-get update >&2
apt-get install -y --no-install-recommends ca-certificates build-essential git libarchive-tools python3 >&2
git config --global --add safe.directory "$PWD"
python3 -B -c 'from pathlib import Path; import sys; from tools.bazel.oci.native_build import audit_command; audit_command(sys.argv[1:], Path.cwd())' "$@"
exec "$@"
'''
    result.extend([ENVIRONMENT, 'sh', '-c', script, 'native-bazel', str(bazel.resolve()),
                   '--output_user_root=' + str(cache / 'bazel'), *startup,
                   *cache_arguments(arguments, shared_cache)])
    return result


def main():
    arguments = sys.argv[1:]
    if arguments != ['--version'] and (not arguments or arguments[0] not in COMMANDS):
        raise SystemExit('Pass one Bazel image build/test/run/query/mod command or --version')
    executable = shutil.which(os.environ.get('BAZEL', 'bazel'))
    if not executable:
        raise SystemExit('Bazel executable was not found')
    if arguments == ['--version']:
        # CI checks the selected Bazel version before resolving any build state.
        # This client-only operation needs neither Docker nor an output lock.
        os.execv(executable, [executable, '--version'])
    startup = json.loads(os.environ.get('BAZEL_NATIVE_STARTUP_ARGS', '[]'))
    if not isinstance(startup, list) or any(not isinstance(item, str) or not item.startswith('--') for item in startup):
        raise SystemExit('BAZEL_NATIVE_STARTUP_ARGS must be a JSON array of Bazel startup options')
    if os.geteuid() == 0 and os.environ.get('APT_INSTALL_BUILD_CONTAINER') == ENVIRONMENT:
        # Direct execution and Make's Docker launcher use the same preflight.
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        shared_cache = Path('/bazel_cache').is_dir()
        arguments = cache_arguments(arguments, shared_cache)
        if shared_cache:
            os.environ['BAZELISK_HOME'] = str(NATIVE_SHARED_CACHE / 'bazelisk')
        audit_command([executable, *startup, *arguments], Path.cwd())
        os.execv(executable, [executable, *startup, *arguments])
    workspace = Path.cwd()
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
    cache = Path(os.environ.get('BAZEL_NATIVE_CACHE_DIR', str(Path.home() / '.cache/sonic-bazel-native' / key))).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with output_lock(cache, startup):
        result = subprocess.run(command(arguments, workspace, Path(executable), cache, startup))
        # Bazel may write this one source-resolution file outside its output cache.
        # Restore the invoking user's ownership before another container starts.
        lock = workspace / 'MODULE.bazel.lock'
        if lock.exists() and lock.stat().st_uid != os.getuid():
            subprocess.run(['docker', 'run', '--rm', '--network=none', '--cap-drop=ALL', '--cap-add=CHOWN',
                            '--mount', 'type=bind,src=' + str(lock.resolve()) + ',dst=/lock',
                            ENVIRONMENT, 'chown', str(os.getuid()) + ':' + str(os.getgid()), '/lock'], check=True)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
