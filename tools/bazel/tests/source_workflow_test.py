"""Execute source-validation workflow shells without compiling source packages."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"
CONTAINER_BUILD_ENVIRONMENT = (
    "BAZEL", "BAZEL_CONTAINER_ARGS", "SONIC_BAZEL_KERNEL_STATE_ROOT", "USER",
    "BUILD_HOSTNAME", "BUILD_TIMESTAMP", "SOURCE_DATE_EPOCH", "SONIC_DEFAULT_CONTAINER_REGISTRY",
)


def jobs():
    """Read top-level job blocks; the shell itself is executed without rewriting."""
    contents = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
    return dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                           contents, re.MULTILINE | re.DOTALL))


def steps(job):
    return dict(re.findall(r"^      - name: ([^\n]+)\n(.*?)(?=^      - |\Z)",
                           job, re.MULTILINE | re.DOTALL))


def shell(step):
    match = re.search(r"^        run: (.*)\n?", step, re.MULTILINE)
    if match is None:
        return None
    if match[1] == "|":
        lines = []
        for line in step[match.end():].splitlines():
            if line and not line.startswith("          "):
                break
            lines.append(line)
        return textwrap.dedent("\n".join(lines))
    return match[1]


class SourceWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-workflow-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.commands = self.directory / "commands"
        self.commands.mkdir()
        (self.directory / "artifacts/vs").mkdir(parents=True)
        (self.directory / "artifacts/vs/cache.bazelrc").write_text("# Prepared by the cache configuration step\n")
        for name in (".bazelversion", "rules/config", "tools/bazel/build_helpers.py",
                     "platform/vs/docker-syncd-vs/bazel/apt.lock.json",
                     "platform/vs/docker-syncd-vs/BUILD.bazel"):
            fixture = self.directory / name
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes((ROOT / name).read_bytes())
        self.calls = self.directory / "calls.jsonl"
        recorder = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
            import json, os, shlex, subprocess, sys
            from pathlib import Path
            name = Path(sys.argv[0]).name
            args = sys.argv[1:]
            with Path(os.environ['CALLS']).open('a') as output:
                output.write(json.dumps({'name': name, 'args': args,
                    'environment': {key: os.environ.get(key) for key in
                                    __BUILD_ENVIRONMENT_KEYS__}}) + '\n')
            if name == 'rm' or 'clean' in args or 'cleanall' in args:
                raise SystemExit('Source validation must preserve existing build state')
            source_runner = name == 'python3' and 'tools/bazel/ci/container.py' in args
            if os.environ.get('HOSTED_CHECKS') == '1':
                if source_runner or (name == 'bazel' and any(arg in ('build', 'test') for arg in args)):
                    raise SystemExit('Hosted regression checks must not compile source packages')
            if name == 'make':
                if args == ['--no-print-directory', '-f', '-']:
                    raise SystemExit(subprocess.run(['/usr/bin/make', *args]).returncode)
                if args[:2] != ['-f', 'slave.mk'] or 'sonic-slave-run' in args:
                    raise SystemExit('Native VS must invoke slave.mk directly')
                raise SystemExit(int(os.environ.get('MAKE_EXIT', '0')))
            if name == 'collect_build_version_files.sh':
                raise SystemExit(int(os.environ.get('COLLECT_EXIT', '0')))
            if name == 'docker':
                state_path = Path(os.environ['CONTAINERS'])
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                operation, *remaining = args
                if operation == 'cp':
                    source, destination = remaining
                    selected, path = destination.split(':', 1)
                    assert selected in state
                    assert path == '/etc/bazel.bazelrc'
                    (state_path.parent / 'copied-cache.bazelrc').write_bytes(Path(source).read_bytes())
                    raise SystemExit(0)
                selected, environment, user = None, {}, None
                while remaining and remaining[0].startswith('-'):
                    option = remaining.pop(0)
                    if option in ('--detach', '--init', '--privileged'):
                        continue
                    value = remaining.pop(0)
                    if option in ('--env', '-e'):
                        key, separator, explicit = value.partition('=')
                        if separator:
                            environment[key] = explicit
                        elif key in os.environ:
                            environment[key] = os.environ[key]
                    elif option == '--name':
                        selected = value
                    elif option == '--user':
                        user = value
                    elif option == '--volume':
                        source, destination, *_ = value.split(':')
                        if destination == '/k':
                            assert Path(source).is_dir(), 'Create the kernel state directory before Docker mounts it'
                    elif option not in ('--device', '--workdir', '--label', '--ulimit'):
                        raise SystemExit('Unmodeled Docker option: ' + option)
                if operation == 'run':
                    assert selected and remaining[0].startswith('debian:trixie-')
                    state[selected] = environment
                    state_path.write_text(json.dumps(state))
                    print('fixture-container-id')
                    raise SystemExit(0)
                if operation != 'exec':
                    raise SystemExit('Unmodeled Docker operation: ' + operation)
                selected, *command = remaining
                assert selected in state, 'The job must reuse its prepared Debian environment'
                if int(os.environ.get('DOCKER_EXEC_EXIT', '0')):
                    raise SystemExit(int(os.environ['DOCKER_EXEC_EXIT']))
                # Exec inherits the environment saved by docker run, not the
                # current host's Bazel settings or credential variables.
                environment = dict(state[selected], **environment)
                for key in ('PATH', 'CALLS', 'CONTAINERS', 'SOURCE_EXIT', 'MAKE_EXIT',
                            'FIXTURE_UID', 'UNSHARE_EXIT', 'BAZEL_INFO_EXIT', 'COLLECT_EXIT'):
                    if key in os.environ:
                        environment[key] = os.environ[key]
                if command == ['bash', 'tools/ci/native_vs/bootstrap.sh']:
                    raise SystemExit(int(os.environ.get('BOOTSTRAP_EXIT', '0')))
                assert user, 'Production Make and verification must use the build user'
                raise SystemExit(subprocess.run(command, env=environment).returncode)
            if source_runner:
                raise SystemExit(int(os.environ.get('SOURCE_EXIT', '0')))
            if name == 'python3' and args[:1] == ['tools/bazel/build_helpers.py']:
                raise SystemExit(subprocess.run([sys.executable, *args]).returncode)
            if name == 'python3' and args[:1] == ['-c']:
                raise SystemExit(subprocess.run([sys.executable, *args]).returncode)
            if name == 'python3' and args == ['-'] and os.environ.get('RUN_PYTHON_STDIN') == '1':
                raise SystemExit(subprocess.run([sys.executable, '-'], input=sys.stdin.read(), text=True).returncode)
            if name == 'python3' and 'tools/bazel/gzip/source_archive.py' in args:
                raise SystemExit(int(os.environ.get('SOURCE_DOWNLOAD_EXIT', '0')))
            if name == 'bazel' and args == ['--version']:
                print('bazel ' + Path('.bazelversion').read_text().strip())
            if name == 'bazel' and args[-2:] == ['info', 'install_base']:
                print(__SANDBOX_INSTALL_BASE__)
                raise SystemExit(int(os.environ.get('BAZEL_INFO_EXIT', '0')))
            if name == 'id':
                assert args in (['-u'], ['-g'])
                print(os.environ.get('FIXTURE_UID', '1000') if args == ['-u'] else '1000')
            if name == 'unshare':
                raise SystemExit(int(os.environ.get('UNSHARE_EXIT', '0')))
            if name == 'hostname':
                print('native-host-fixture')
            if name == 'git':
                if args[:2] == ['rev-parse', 'HEAD']:
                    print('a' * 40)
                elif args[:1] == ['check-ignore']:
                    print('MODULE.bazel.lock')
                elif args == ['log', '-1', '--format=%ct', 'HEAD']:
                    print('1728000000')
            ''').replace('__SANDBOX_INSTALL_BASE__', repr(str(self.directory / 'bazel-install'))).replace(
                '__BUILD_ENVIRONMENT_KEYS__', repr(CONTAINER_BUILD_ENVIRONMENT))
        for name in ("make", "docker", "python3", "bazel", "apt-get", "git", "df", "du", "rm", "cp", "id", "unshare", "hostname"):
            executable = self.commands / name
            executable.write_text(recorder)
            executable.chmod(0o755)
        collector = self.directory / "scripts/collect_build_version_files.sh"
        collector.parent.mkdir()
        collector.write_text(recorder)
        collector.chmod(0o755)
        self.sandbox_status = self.directory / "sandbox-status.json"
        sandbox = self.directory / "bazel-install/linux-sandbox"
        sandbox.parent.mkdir()
        sandbox.write_text("#!" + sys.executable + "\n" + textwrap.dedent(r'''
            import json, sys
            from pathlib import Path
            args = sys.argv[1:]
            # Read the initial environment: Python can add LC_CTYPE after exec.
            environment_keys = sorted(entry.split(b'=', 1)[0].decode() for entry in
                                      Path('/proc/self/environ').read_bytes().split(b'\0') if entry)
            with Path(__CALLS__).open('a') as output:
                output.write(json.dumps({'name': 'linux-sandbox', 'args': args,
                                         'environment_keys': environment_keys}) + '\n')
            status = json.loads(Path(__STATUS__).read_text())
            raise SystemExit(status['network' if '-N' in args else 'normal'])
            ''').replace('__CALLS__', repr(str(self.calls))).replace('__STATUS__', repr(str(self.sandbox_status))))
        sandbox.chmod(0o755)
        self.environment = dict(
            os.environ, PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            CALLS=str(self.calls), BAZEL=str(self.commands / "bazel"),
            CONTAINERS=str(self.directory / "containers.json"),
            BAZEL_CONTAINER_ARGS="--distdir='/bazel_cache/source downloads' --remote_download_all",
            BAZEL_CONTAINER_CACHE_DIR=str(self.directory / "build-cache"),
            SONIC_BAZEL_REMOTE_CACHE="https://lgh-bazel-87263989.eastus.cloudapp.azure.com/",
            VS_BUILD_CONTAINER="native-vs-test",
            SONIC_DPKG_CACHE_METHOD="none",
            SONIC_BAZEL_CACHE_SOURCE="",
            GITHUB_WORKSPACE=str(self.directory), GITHUB_OUTPUT=str(self.directory / "github-output"),
            GITHUB_ENV=str(self.directory / "github-env"),
            GITHUB_STEP_SUMMARY=str(self.directory / "github-summary"),
            SONIC_CI_CONTAINER_LABEL="test-source-validation=one-job",
        )
        for variable in ("DEFAULT_CONTAINER_REGISTRY", "SOURCE_DATE_EPOCH"):
            self.environment.pop(variable, None)

    def execute(self, script, **environment):
        # GitHub evaluates these scalar expressions before invoking bash.
        script = script.replace("${{ github.run_id }}", "1001").replace("${{ github.run_attempt }}", "2")
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                              cwd=self.directory, env=dict(self.environment, **environment),
                              capture_output=True, text=True)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def prepare_container(self):
        result = self.execute(shell(steps(jobs()["vs"])["Prepare plain Debian execution environment"]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        docker = [call for call in self.recorded() if call["name"] == "docker" and call["args"][0] == "run"]
        self.assertEqual(len(docker), 1)
        self.assertEqual(docker[0]["args"][docker[0]["args"].index("--ulimit") + 1], "nofile=524288:524288")
        mounts = [docker[0]["args"][index + 1] for index, argument in enumerate(docker[0]["args"])
                  if argument == "--volume"]
        self.assertIn(str(self.directory / "target/bazel-kernel-state") + ":/k", mounts)
        environment = json.loads((self.directory / "containers.json").read_text())[self.environment["VS_BUILD_CONTAINER"]]
        self.assertEqual(environment["SONIC_BAZEL_KERNEL_STATE_ROOT"], "/k")
        self.calls.unlink()
        return environment

    def test_hosted_regressions_gate_vs_without_source_compilation(self):
        """Cheap hosted checks gate the full VS job without compiling its source closure twice."""
        workflow_jobs = jobs()
        self.assertNotIn("source", workflow_jobs)
        self.assertNotIn("syncd-contracts", workflow_jobs)
        checks = workflow_jobs["checks"]
        self.assertIn("name: OCI regression checks (AMD64)", checks)
        self.assertIn("runs-on: ubuntu-24.04", checks)
        self.assertRegex(workflow_jobs["vs"], r"(?m)^    needs: (?:checks|\[checks\])$")
        for name, step in steps(checks).items():
            script = shell(step)
            if script is not None:
                with self.subTest(step=name):
                    result = self.execute(script, HOSTED_CHECKS="1")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded()
        self.assertTrue(any(call["name"] == "python3" and
                            "unittest" in call["args"] for call in calls))
        self.assertFalse(any("tools/bazel/ci/container.py" in call["args"] or
                             (call["name"] == "bazel" and
                              any(arg in ("build", "test") for arg in call["args"]))
                             for call in calls))

    def test_make_source_validation_reuses_outputs_and_propagates_failures(self):
        """Both owners reuse Make outputs in the same environment and retain failure status."""
        workflow_steps = steps(jobs()["vs"])
        container_environment = self.prepare_container()
        names = list(workflow_steps)
        families = (
            ("SWSS", "//dockers/docker-orchagent", "rules/docker-orchagent.mk", "artifacts/swss", False),
            ("Syncd", "//platform/vs/docker-syncd-vs", "platform/vs/docker-syncd-vs.mk", "artifacts/syncd-vs/bazel", True),
        )
        lock = self.directory / "MODULE.bazel.lock"
        lock.write_text("already-resolved Make build\n")
        for family, owner, source, artifacts, dash in families:
            name = "Validate " + family + " source packages from the Make build"
            self.assertLess(names.index("Build the VS image and both OCI container variants"), names.index(name))
            for source_exit, docker_exit in ((0, 0), (19, 0), (0, 23)):
                with self.subTest(family=family, source_exit=source_exit, docker_exit=docker_exit):
                    self.calls.unlink(missing_ok=True)
                    result = self.execute(shell(workflow_steps[name]),
                                          SOURCE_EXIT=str(source_exit), DOCKER_EXEC_EXIT=str(docker_exit),
                                          BAZEL="host-bazel-must-not-leak",
                                          BAZEL_CONTAINER_ARGS="--host-option-must-not-leak")
                    self.assertEqual(result.returncode, source_exit or docker_exit,
                                     result.stdout + result.stderr)
                    self.assertEqual(lock.read_text(), "already-resolved Make build\n")
                    calls = self.recorded()
                    self.assertFalse(any(call["name"] == "make" for call in calls))
                    docker = [call for call in calls if call["name"] == "docker"]
                    self.assertEqual(len(docker), 1)
                    self.assertEqual(docker[0]["args"][:2], ["exec", "--user"])
                    self.assertEqual(docker[0]["args"][-3:], [self.environment["VS_BUILD_CONTAINER"], "bash",
                                                                         "artifacts/vs/check-" + family.lower() + "-source.sh"])
                    runner = [call for call in calls if "tools/bazel/ci/container.py" in call["args"]]
                    self.assertEqual(len(runner), 0 if docker_exit else 1)
                    if runner:
                        args = runner[0]["args"]
                        self.assertIn("--reuse-build", args)
                        for flag, value in (("--container", owner), ("--source-file", source),
                                            ("--artifacts", artifacts), ("--bazel", container_environment["BAZEL"])):
                            self.assertEqual(args[args.index(flag) + 1], value)
                        self.assertEqual("--bazel-arg=--config=syncd-dash" in args, dash)
                        self.assertEqual("--bazel-arg=--//dockers/docker-orchagent:source_config_engine=true" in args,
                                         not dash)
                        for option in ("--distdir=/bazel_cache/source downloads", "--remote_download_all",
                                       "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
                                       "--host_platform=@sonic_build_infra//platforms:x86_64_trixie"):
                            self.assertIn("--bazel-arg=" + option, args)
                        self.assertEqual(runner[0]["environment"],
                                         {key: container_environment[key] for key in CONTAINER_BUILD_ENVIRONMENT})
                        self.assertFalse(any(arg.startswith("--bazel-arg=--disk_cache=") for arg in args))
                    analyses = [call for call in calls if "tools/bazel/ci/bazel_commands.py" in call["args"]]
                    self.assertEqual(len(analyses), int(dash and not source_exit and not docker_exit))
                    self.assertFalse(any(call["name"] == "rm" or "clean" in call["args"] for call in calls))

    def test_direct_make_keeps_full_vs_targets_and_failure_status(self):
        """Changing the execution environment must retain the KVM image and both OCI variants."""
        self.prepare_container()
        script = shell(steps(jobs()["vs"])["Build the VS image and both OCI container variants"])
        for make_exit, docker_exit in ((0, 0), (17, 0), (0, 29)):
            with self.subTest(make_exit=make_exit, docker_exit=docker_exit):
                self.calls.unlink(missing_ok=True)
                result = self.execute(script, MAKE_EXIT=str(make_exit), DOCKER_EXEC_EXIT=str(docker_exit),
                                      SONIC_BAZEL_KERNEL_STATE_ROOT="/host-value-must-not-leak")
                self.assertEqual(result.returncode, make_exit or docker_exit, result.stdout + result.stderr)
                make = [call for call in self.recorded() if call["name"] == "make"]
                self.assertEqual(len(make), 0 if docker_exit else 1)
                if make:
                    self.assertEqual(make[0]["args"][:2], ["-f", "slave.mk"])
                    self.assertEqual(make[0]["environment"]["SONIC_BAZEL_KERNEL_STATE_ROOT"], "/k")
                    for target in ("target/sonic-vs.img.gz", "target/docker-orchagent.gz",
                                   "target/docker-orchagent-dbg.gz", "target/docker-syncd-vs.gz",
                                   "target/docker-syncd-vs-dbg.gz",
                                   "target/debs/trixie/sonic-p4rt_0.0.1_amd64.deb",
                                   "target/debs/trixie/sonic-p4rt-dbgsym_0.0.1_amd64.deb",
                                   "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y"):
                        self.assertIn(target, make[0]["args"])
        job = jobs()["vs"]
        self.assertIn("runs-on: ubuntu-22.04", job)
        self.assertNotIn("self-hosted", job)
        self.assertNotIn("sonic-slave-run", job)
        self.assertNotIn("Makefile.work", job)
        self.assertNotIn("USE_BAZEL_VERSION", job)

    def test_native_environment_keeps_registry_precedence_and_build_identity(self):
        """Direct Make must retain wrapper registry overrides, ownership and reproducible timestamps."""
        build = shell(steps(jobs()["vs"])["Build the VS image and both OCI container variants"])
        cases = (
            (None, None, None, "publicmirror.azurecr.io/"),
            ("organization.test/team///", None, None, "organization.test/team/"),
            ("organization.test", "user.test/team/", "1600000000", "user.test/team/"),
            ("organization.test", "", "1600000001", ""),
            (None, "registry.test/owner's;`touch registry-command-executed`", None,
             "registry.test/owner's;`touch registry-command-executed`/"),
        )
        for organization, user, epoch, expected_registry in cases:
            with self.subTest(organization=organization, user=user, epoch=epoch):
                self.calls.unlink(missing_ok=True)
                for filename, value in (("config.organization", organization), ("config.user", user)):
                    config = self.directory / "rules" / filename
                    if value is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text("DEFAULT_CONTAINER_REGISTRY := " + value + "\n")
                if epoch is None:
                    self.environment.pop("SOURCE_DATE_EPOCH", None)
                else:
                    self.environment["SOURCE_DATE_EPOCH"] = epoch
                environment = self.prepare_container()
                self.assertEqual(environment["USER"], "vsbuild")
                self.assertEqual(environment["BUILD_HOSTNAME"], "native-host-fixture")
                self.assertRegex(environment["BUILD_TIMESTAMP"], r"^\d{8}\.\d{6}$")
                self.assertEqual(environment["SOURCE_DATE_EPOCH"], epoch or "1728000000")
                self.assertEqual(environment["SONIC_DEFAULT_CONTAINER_REGISTRY"], expected_registry)
                self.assertFalse((self.directory / "registry-command-executed").exists())
                result = self.execute(build, USER="host-user", BUILD_HOSTNAME="host-name",
                                      BUILD_TIMESTAMP="host-time", SOURCE_DATE_EPOCH="host-epoch",
                                      SONIC_DEFAULT_CONTAINER_REGISTRY="host-registry/")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                make = [call for call in self.recorded() if call["name"] == "make"]
                self.assertEqual(len(make), 1)
                self.assertEqual(make[0]["environment"],
                                 {key: environment[key] for key in CONTAINER_BUILD_ENVIRONMENT})
        # A configuration error must fail before creating the build environment.
        self.calls.unlink()
        (self.directory / "rules/config.user").write_text("$(error invalid registry fixture)\n")
        failed = self.execute(shell(steps(jobs()["vs"])["Prepare plain Debian execution environment"]))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("invalid registry fixture", failed.stderr)
        self.assertFalse(any(call["name"] == "docker" for call in self.recorded()))

    def test_version_collection_runs_before_cleanup_even_after_build_failure(self):
        """Retain package versions while the execution container still exists, including failed builds."""
        workflow_steps = steps(jobs()["vs"])
        name = "Collect native build versions"
        names = list(workflow_steps)
        self.assertLess(names.index("Build the VS image and both OCI container variants"), names.index(name))
        self.assertLess(names.index(name), names.index("Remove this job's remaining build containers"))
        self.assertIn("id: native_environment", workflow_steps["Prepare plain Debian execution environment"])
        self.assertRegex(workflow_steps[name],
                         r"(?m)^        if: always\(\) && steps\.native_environment\.outcome == 'success'$")
        environment = self.prepare_container()
        for collector_exit, docker_exit in ((0, 0), (47, 0), (0, 49)):
            with self.subTest(collector_exit=collector_exit, docker_exit=docker_exit):
                self.calls.unlink(missing_ok=True)
                result = self.execute(shell(workflow_steps[name]), COLLECT_EXIT=str(collector_exit),
                                      DOCKER_EXEC_EXIT=str(docker_exit))
                self.assertEqual(result.returncode, collector_exit or docker_exit, result.stdout + result.stderr)
                calls = self.recorded()
                docker = [call for call in calls if call["name"] == "docker"]
                self.assertEqual(len(docker), 1)
                self.assertEqual(docker[0]["args"], ["exec", "--user", "vsbuild", self.environment["VS_BUILD_CONTAINER"],
                                                    "scripts/collect_build_version_files.sh", "0", "trixie", "target"])
                collector = [call for call in calls if call["name"] == "collect_build_version_files.sh"]
                self.assertEqual(len(collector), int(not docker_exit))
                if collector:
                    self.assertEqual(collector[0]["args"], ["0", "trixie", "target"])
                    self.assertEqual(collector[0]["environment"],
                                     {key: environment[key] for key in CONTAINER_BUILD_ENVIRONMENT})

    def test_unprivileged_sandbox_probes_gate_make_and_preserve_failures(self):
        """Reject unusable user/network namespaces before configure or packaging can start."""
        workflow_steps = steps(jobs()["vs"])
        probe = "Require the unprivileged Bazel Linux sandbox"
        configure = "Configure the native VS image"
        build = "Build the VS image and both OCI container variants"
        names = list(workflow_steps)
        self.assertLess(names.index("Prepare plain Debian execution environment"), names.index(probe))
        self.assertLess(names.index(probe), names.index(configure))
        self.assertLess(names.index(configure), names.index(build))
        self.prepare_container()
        # Model the bind-mounted Bazel executable without creating a container filesystem.
        state_path = self.directory / "containers.json"
        state = json.loads(state_path.read_text())
        state[self.environment["VS_BUILD_CONTAINER"]]["BAZEL"] = str(self.commands / "bazel")
        state_path.write_text(json.dumps(state))
        script = "\n".join(shell(workflow_steps[name]) for name in (probe, configure, build))
        cases = (
            ("usable", "1000", 0, 0, 0, 0, 0, 2),
            ("root", "0", 0, 0, 0, 0, 1, 0),
            ("namespace-denied", "1000", 37, 0, 0, 0, 37, 0),
            ("bazel-info-failed", "1000", 0, 39, 0, 0, 39, 0),
            ("sandbox-failed", "1000", 0, 0, 41, 0, 41, 1),
            ("network-sandbox-failed", "1000", 0, 0, 0, 43, 43, 2),
        )
        for label, uid, unshare_exit, bazel_exit, normal_exit, network_exit, expected_exit, probes in cases:
            with self.subTest(case=label):
                self.calls.unlink(missing_ok=True)
                self.sandbox_status.write_text(json.dumps({"normal": normal_exit, "network": network_exit}))
                result = self.execute(script, FIXTURE_UID=uid, UNSHARE_EXIT=str(unshare_exit),
                                      BAZEL_INFO_EXIT=str(bazel_exit))
                self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                calls = self.recorded()
                make = [call for call in calls if call["name"] == "make"]
                self.assertEqual(len(make), 0 if expected_exit else 2)
                unshare = [call for call in calls if call["name"] == "unshare"]
                self.assertEqual(len(unshare), int(uid != "0"))
                if unshare:
                    self.assertEqual(unshare[0]["args"], ["--user", "--map-current-user", "--mount",
                                                         "--ipc", "--uts", "--net", "--pid", "--fork", "/bin/true"])
                bazel = [call for call in calls if call["name"] == "bazel"]
                self.assertEqual(len(bazel), int(uid != "0" and not unshare_exit))
                if bazel:
                    self.assertEqual(bazel[0]["args"], ["--batch", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
                                                       "--output_user_root=" + str(self.directory / "target/native-bazel-preflight"),
                                                       "info", "install_base"])
                helpers = [call for call in calls if call["name"] == "linux-sandbox"]
                self.assertEqual(len(helpers), probes)
                for helper in helpers:
                    self.assertEqual(helper["environment_keys"], [], "The embedded sandbox must run with env -i")
                if helpers:
                    self.assertEqual(helpers[0]["args"], ["-T", "5", "-D", str(self.directory / "artifacts/vs/sandbox-probe.log"),
                                                         "--", "/bin/true"])
                if len(helpers) == 2:
                    self.assertEqual(helpers[1]["args"], ["-T", "5", "-N", "--", "/bin/true"])

    def test_lgh_configuration_applies_to_direct_bazel_and_make_without_writer_credentials(self):
        """Cache reads reach both invocation paths without exporting writer secrets or enabling uploads."""
        script = shell(steps(jobs()["vs"])["Configure LGH cache reads and verified source downloads"])
        result = self.execute(script, RUN_PYTHON_STDIN="1", BAZEL_REMOTE_AUTH="fixture-writer-secret")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        persisted = (self.directory / "github-env").read_text()
        self.assertNotIn("fixture-writer-secret", persisted)
        self.assertNotIn("fixture-writer-secret", (self.directory / ".bazelrc.user").read_text())
        name, options = persisted.strip().split("=", 1)
        self.assertEqual(name, "BAZEL_CONTAINER_ARGS")
        options = shlex.split(options)
        rc_options = []
        for line in (self.directory / ".bazelrc.user").read_text().splitlines():
            command, option = shlex.split(line)
            self.assertEqual(command, "build")
            rc_options.append(option)
        self.assertEqual(options, rc_options)
        cache_options = ["--remote_cache=" + self.environment["SONIC_BAZEL_REMOTE_CACHE"],
                         "--remote_accept_cached=true", "--remote_upload_local_results=false",
                         "--remote_executor=", "--incompatible_strict_action_env"]
        for option in cache_options:
            self.assertIn(option, options)
        system_rc = self.directory / "artifacts/vs/cache.bazelrc"
        self.assertEqual([shlex.split(line) for line in system_rc.read_text().splitlines()],
                         [["build", option] for option in cache_options])
        self.assertNotIn("fixture-writer-secret", system_rc.read_text())
        self.assertFalse(any(option.startswith("--disk_cache=") for option in options))
        self.assertEqual(sum(option.startswith("--distdir=") for option in options), 5)
        self.environment["BAZEL_CONTAINER_ARGS"] = shlex.join(options)
        self.environment["BAZEL_REMOTE_AUTH"] = "fixture-writer-secret"
        self.environment["GITHUB_TOKEN"] = "fixture-github-secret"
        container_environment = self.prepare_container()
        self.assertEqual(shlex.split(container_environment["BAZEL_CONTAINER_ARGS"]), options)
        self.assertEqual(container_environment["SONIC_BAZEL_REMOTE_CACHE"], self.environment["SONIC_BAZEL_REMOTE_CACHE"])
        self.assertRegex(jobs()["vs"], r'(?m)^      SONIC_BAZEL_CACHE_SOURCE: ""$')
        self.assertEqual(container_environment["SONIC_BAZEL_CACHE_SOURCE"], "")
        self.assertNotIn("BAZEL_CONTAINER_CACHE_DIR", container_environment)
        # An empty exported value must suppress rules/config's /bazel_cache
        # fallback even though Make's default package cache directory is set.
        config = subprocess.run(
            ["/usr/bin/make", "--no-print-directory", "-f", "-"], cwd=ROOT,
            env=dict(os.environ, SONIC_BAZEL_CACHE_SOURCE=container_environment["SONIC_BAZEL_CACHE_SOURCE"]),
            input="include rules/config\nall:\n\t@test -z '$(SONIC_BAZEL_CACHE_SOURCE)'\n",
            text=True, capture_output=True,
        )
        self.assertEqual(config.returncode, 0, config.stdout + config.stderr)
        self.assertNotIn("BAZEL_REMOTE_AUTH", container_environment)
        self.assertNotIn("GITHUB_TOKEN", container_environment)
        self.assertNotIn("USE_BAZEL_VERSION", container_environment)
        self.assertEqual((self.directory / "copied-cache.bazelrc").read_bytes(), system_rc.read_bytes())
        failed = self.execute(script, RUN_PYTHON_STDIN="1", SOURCE_DOWNLOAD_EXIT="31")
        self.assertEqual(failed.returncode, 31, failed.stdout + failed.stderr)


if __name__ == "__main__":
    unittest.main()
