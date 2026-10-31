"""Execute source-validation workflow shells without compiling source packages."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def jobs(workflow=WORKFLOW):
    """Read top-level job blocks; the shell itself is executed without rewriting."""
    contents = workflow.read_text().split("\njobs:\n", 1)[1]
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
        self.directory = Path(temporary.name).resolve()
        self.commands = self.directory / "commands"
        self.commands.mkdir()
        (self.directory / "artifacts/vs").mkdir(parents=True)
        for name in (".bazelversion", "tools/bazel/build_helpers.py",
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
                                    ('BAZEL', 'BAZEL_CONTAINER_ARGS')}}) + '\n')
            if name == 'rm' or 'clean' in args or 'cleanall' in args:
                raise SystemExit('Source validation must preserve existing build state')
            source_runner = name == 'python3' and 'tools/bazel/ci/container.py' in args
            if os.environ.get('HOSTED_CHECKS') == '1':
                if source_runner or (name == 'bazel' and any(arg in ('build', 'test') for arg in args)):
                    raise SystemExit('Hosted regression checks must not compile source packages')
            if name == 'make' and 'sonic-slave-run' in args:
                if int(os.environ.get('MAKE_EXIT', '0')):
                    raise SystemExit(int(os.environ['MAKE_EXIT']))
                values = dict(arg.split('=', 1) for arg in args if '=' in arg)
                # Model Docker's explicit -e forwarding instead of inheriting
                # the outer workflow's Bazel settings by accident.
                environment = dict(os.environ)
                for key in ('BAZEL', 'BAZEL_CONTAINER_ARGS'):
                    environment.pop(key, None)
                extra = shlex.split(values.get('SONIC_BUILDER_EXTRA_CMDLINE', ''))
                for index, arg in enumerate(extra):
                    if arg == '-e':
                        key, separator, value = extra[index + 1].partition('=')
                        environment[key] = value if separator else os.environ[key]
                raise SystemExit(subprocess.run(
                    ['bash', '-e', '-o', 'pipefail', '-c', values['SONIC_RUN_CMDS']],
                    env=environment).returncode)
            if source_runner:
                raise SystemExit(int(os.environ.get('SOURCE_EXIT', '0')))
            if name == 'python3' and 'tools/bazel/kernel/prepare.py' in args:
                raise SystemExit(int(os.environ.get('KERNEL_PREPARE_EXIT', '0')))
            if name == 'python3' and 'tools/bazel/ci/kernel_handoff.py' in args:
                raise SystemExit(int(os.environ.get('KERNEL_HANDOFF_EXIT', '0')))
            if name == 'python3' and 'tools/bazel/gzip/source_archive.py' in args:
                if '--fetch-arguments' in args:
                    print('--repo=@@first+\n--repo=@@rules_m4++default_toolchain_ext+m4_v1.4.18')
                    raise SystemExit(int(os.environ.get('SOURCE_FETCH_EXIT', '0')))
                if '--distdir-arguments' in args:
                    prefix = args[args.index('--distdir-prefix') + 1]
                    print('--distdir=' + prefix + '/first-1.0\n--distdir=' + prefix + '/second-2.0')
                    raise SystemExit(int(os.environ.get('SOURCE_DISTDIR_EXIT', '0')))
                output = Path(args[args.index('--output') + 1])
                print(json.dumps([{'archive': str(output / relative)} for relative in
                                  ('first-1.0/src.tar.gz', 'second-2.0/source.tar.xz')]))
                raise SystemExit(int(os.environ.get('SOURCE_ARCHIVE_EXIT', '0')))
            if name == 'python3' and args == ['-']:
                script = sys.stdin.read()
                if 'paths<<SOURCE_ARCHIVES' in script:
                    raise SystemExit(subprocess.run([sys.executable, '-'], input=script, text=True).returncode)
            if name == 'python3' and args[:1] == ['tools/bazel/build_helpers.py']:
                raise SystemExit(subprocess.run([sys.executable, *args]).returncode)
            if name == 'bazel' and args == ['--version']:
                print('bazel ' + Path('.bazelversion').read_text().strip())
            if name == 'bazel' and args[:1] == ['fetch']:
                raise SystemExit(int(os.environ.get('BAZEL_FETCH_EXIT', '0')))
            if name == 'bazel' and args == ['info', 'repository_cache']:
                print(str(Path.cwd() / 'repository-cache'))
            if name == 'git':
                if args[:2] == ['rev-parse', 'HEAD']:
                    print('a' * 40)
                elif args[:1] == ['check-ignore']:
                    print('MODULE.bazel.lock')
            ''')
        for name in ("make", "python3", "bazel", "apt-get", "git", "df", "du", "rm", "cp"):
            executable = self.commands / name
            executable.write_text(recorder)
            executable.chmod(0o755)
        self.environment = dict(
            os.environ, PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            CALLS=str(self.calls), BAZEL=str(self.commands / "bazel"),
            BAZEL_CONTAINER_ARGS="--distdir='/bazel_cache/source downloads' --remote_download_all",
            BAZEL_CONTAINER_CACHE_DIR=str(self.directory / "build-cache"),
            GITHUB_WORKSPACE=str(self.directory), GITHUB_OUTPUT=str(self.directory / "github-output"),
            GITHUB_ENV=str(self.directory / "github-env"),
            GITHUB_STEP_SUMMARY=str(self.directory / "github-summary"),
            SONIC_BAZEL_CACHE_SOURCE=str(self.directory / "persistent-cache"),
            SONIC_CI_CONTAINER_LABEL="test-source-validation=one-job",
        )

    def execute(self, script, **environment):
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                              cwd=self.directory, env=dict(self.environment, **environment),
                              capture_output=True, text=True)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_hosted_regressions_gate_vs_without_source_compilation(self):
        workflow_jobs = jobs()
        self.assertNotIn("source", workflow_jobs)
        self.assertNotIn("syncd-contracts", workflow_jobs)
        checks = workflow_jobs["checks"]
        self.assertIn("name: OCI regression checks (AMD64)", checks)
        self.assertIn("runs-on: ubuntu-24.04", checks)
        self.assertRegex(workflow_jobs["vs"], r"(?m)^    needs: \[checks, kernel-native\]$")
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

    def test_hosted_downloads_use_reviewed_selectors_and_publish_only_verified_paths(self):
        for filename, job, title in (
                ("bazel-oci.yml", "checks", "Retain the verified source downloads"),
                ("bazel-vs-unprivileged.yml", "store", "Fetch and verify the source archives")):
            selected = steps(jobs(ROOT / ".github/workflows" / filename)[job])
            script = shell(selected[title])
            for selector_exit, fetch_exit, archive_exit in ((0, 0, 0), (7, 0, 0), (0, 11, 0), (0, 0, 13)):
                with self.subTest(workflow=filename, selector=selector_exit, fetch=fetch_exit, archive=archive_exit):
                    self.calls.unlink(missing_ok=True)
                    output = Path(self.environment["GITHUB_OUTPUT"])
                    output.unlink(missing_ok=True)
                    result = self.execute(script, SOURCE_FETCH_EXIT=str(selector_exit),
                                          BAZEL_FETCH_EXIT=str(fetch_exit), SOURCE_ARCHIVE_EXIT=str(archive_exit))
                    expected = selector_exit or fetch_exit or archive_exit
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    calls = self.recorded()
                    fetches = [call for call in calls if call["name"] == "bazel" and call["args"][:1] == ["fetch"]]
                    self.assertEqual(len(fetches), 0 if selector_exit else 1)
                    if fetches:
                        self.assertEqual(fetches[0]["args"], ["fetch", "--repo=@@first+",
                                         "--repo=@@rules_m4++default_toolchain_ext+m4_v1.4.18"])
                    exports = [call for call in calls if "--repository-cache" in call["args"]]
                    self.assertEqual(len(exports), 0 if selector_exit or fetch_exit else 1)
                    if filename == "bazel-oci.yml" and not expected:
                        receipt = json.loads((self.directory / "artifacts/repository-inputs/receipt.json").read_text())
                        self.assertEqual(output.read_text().splitlines(),
                                         ["paths<<SOURCE_ARCHIVES", *[item["archive"] for item in receipt], "SOURCE_ARCHIVES"])
                    else:
                        self.assertFalse(output.exists(), "failed source verification published artifact paths")
            if filename == "bazel-oci.yml":
                upload = selected["Upload the source downloads for the VS job"]
                self.assertIn("${{ steps.source_downloads.outputs.paths }}", upload)
                self.assertIn("artifacts/repository-inputs/receipt.json", upload)
                self.assertIn("artifacts/repository-inputs/test-helpers/", upload)
                self.assertNotIn("module-graph", upload)
                self.assertNotRegex(upload, r"(?m)^\s+artifacts/repository-inputs/\s*$")

    def test_native_source_arguments_are_published_only_after_complete_verification(self):
        for filename, job, title in (
                ("bazel-oci.yml", "vs", "Prepare the shared Bazel source-download directory"),
                ("bazel-vs-unprivileged.yml", "native", "Prepare the native Bazel source-download directory")):
            native = jobs(ROOT / ".github/workflows" / filename)[job]
            self.assertNotIn("      BAZEL_CONTAINER_ARGS:", native)
            script = shell(steps(native)[title])
            for archive_exit, distdir_exit in ((0, 0), (17, 0), (0, 19)):
                with self.subTest(workflow=filename, archive=archive_exit, distdir=distdir_exit):
                    self.calls.unlink(missing_ok=True)
                    environment = Path(self.environment["GITHUB_ENV"])
                    environment.unlink(missing_ok=True)
                    result = self.execute(script, SOURCE_ARCHIVE_EXIT=str(archive_exit), SOURCE_DISTDIR_EXIT=str(distdir_exit))
                    expected = archive_exit or distdir_exit
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    verifiers = [call for call in self.recorded() if "--distdir-arguments" in call["args"]]
                    self.assertEqual(len(verifiers), 0 if archive_exit else 1)
                    if verifiers:
                        self.assertEqual(verifiers[0]["args"], ["-B", "tools/bazel/gzip/source_archive.py",
                                         "--distdir-arguments", str(Path(self.environment["SONIC_BAZEL_CACHE_SOURCE"]) / "distdir"),
                                         "--distdir-prefix", "/bazel_cache/distdir"])
                    if expected:
                        self.assertFalse(environment.exists(), "failed verification published Bazel source arguments")
                    else:
                        self.assertEqual(environment.read_text(), "BAZEL_CONTAINER_ARGS="
                                         "--distdir=/bazel_cache/distdir/first-1.0 "
                                         "--distdir=/bazel_cache/distdir/second-2.0\n")

    def test_native_jobs_import_same_revision_kernel_packages_before_make(self):
        for filename, job_name, prerequisite, prefix in (
                ("bazel-oci.yml", "vs", "checks", "vs"),
                ("bazel-vs-unprivileged.yml", "native", "store", "native")):
            with self.subTest(workflow=filename):
                blocks = jobs(ROOT / ".github/workflows" / filename)
                caller, native = blocks["kernel-native"], blocks[job_name]
                self.assertIn("uses: ./.github/workflows/bazel-kernel-native.yml", caller)
                self.assertIn(f"needs: [{prerequisite}, kernel-native]", native)
                condition = r"(?ms)^    if: (.*?)(?=^    \S|\Z)"
                self.assertEqual(re.search(condition, caller)[1], re.search(condition, native)[1])
                self.assertIn("ref: ${{ github.sha }}", native)
                self.assertIn("submodules: recursive", native)
                selected = steps(native)
                download = selected["Download this run's verified kernel packages"]
                self.assertIn("name: kernel-native-amd64", download)
                self.assertIn("path: " + prefix + "-${{ github.run_id }}-${{ github.run_attempt }}/artifacts/kernel-native-amd64", download)
                self.assertNotIn("run-id:", download)
                self.assertLess(native.index("uses: actions/checkout@"), native.index("Download this run's verified kernel packages"))
                self.assertLess(native.index("Verify and import the kernel packages before native Make"), native.index("make init"))
                script = shell(selected["Verify and import the kernel packages before native Make"])
                self.calls.unlink(missing_ok=True)
                result = self.execute(script)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.recorded()[0]["args"], [
                    "-B", "tools/bazel/ci/kernel_handoff.py", "import", "--workspace", str(self.directory),
                    "--input", "artifacts/kernel-native-amd64", "--debs-directory", "target/debs/trixie"])
                result = self.execute(script, KERNEL_HANDOFF_EXIT="17")
                self.assertEqual(result.returncode, 17, result.stdout + result.stderr)

    def test_hosted_kernel_producer_exports_only_after_verified_build(self):
        producer = jobs(ROOT / ".github/workflows/bazel-kernel-native.yml")["kernel"]
        self.assertIn("runs-on: ubuntu-22.04", producer)
        self.assertIn("ref: ${{ github.sha }}", producer)
        self.assertNotIn("pull_request.head.sha", producer)
        selected = steps(producer)
        self.assertIn("src/sonic-linux-kernel", shell(selected["Initialize the pinned kernel source"]))
        self.assertIn("unshare --user --map-current-user --mount --pid --fork true",
                      shell(selected["Require unprivileged Linux sandboxing"]))
        self.assertNotIn("sysctl", producer)
        self.assertNotIn("apparmor_parser", producer)
        script = shell(selected["Build and verify the VS kernel packages"])
        result = self.execute(script, RUNNER_TEMP=str(self.directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded()
        self.assertEqual(len(calls), 2)
        self.assertIn("tools/bazel/kernel/prepare.py", calls[0]["args"])
        self.assertIn("ensure", calls[0]["args"])
        for setting in ("CONFIGURED_ARCH=amd64", "CONFIGURED_PLATFORM=vs", "BLDENV=trixie", "SECURE_UPGRADE_MODE=no_sign"):
            self.assertIn(setting, calls[0]["args"])
        self.assertEqual(calls[1]["args"], [
            "-B", "tools/bazel/ci/kernel_handoff.py", "export", "--workspace", str(self.directory),
            "--debs-directory", "target/debs/trixie", "--output", str(self.directory / "kernel-native-amd64")])
        self.calls.unlink()
        result = self.execute(script, RUNNER_TEMP=str(self.directory), KERNEL_PREPARE_EXIT="19")
        self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
        self.assertEqual(len(self.recorded()), 1, "failed kernel build reached artifact export")
        transfer = selected["Transfer the verified kernel packages to native Make"]
        self.assertIn("name: kernel-native-amd64", transfer)
        self.assertNotIn("always()", transfer)

    def test_kernel_cache_bound_counts_only_download_and_action_files(self):
        selected = steps(jobs(ROOT / ".github/workflows/bazel-kernel-native.yml")["kernel"])
        script = shell(selected["Bound the retained kernel cache"])
        cache = self.directory / "kernel-native-cache/kernel-linux-sandbox"
        blob = cache / "disk_cache/blob"
        blob.parent.mkdir(parents=True)
        # Sparse files exercise the real logical-size limit without allocating it.
        ignored = self.directory / "kernel-native-state/build/output"
        ignored.parent.mkdir(parents=True)
        with ignored.open("wb") as output:
            output.truncate(16 * 2**30)
        for size, expected in ((0, False), (1024, True), (8 * 2**30, True), (8 * 2**30 + 1, False)):
            with self.subTest(bytes=size):
                with blob.open("wb") as output:
                    output.truncate(size)
                github_output = self.directory / "cache-output"
                github_output.unlink(missing_ok=True)
                result = self.execute(script, RUNNER_TEMP=str(self.directory), GITHUB_OUTPUT=str(github_output),
                                      PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(github_output.read_text(), "save=" + str(expected).lower() + "\n")
        for title in ("Restore kernel download and action caches", "Save the bounded kernel cache"):
            block = selected[title]
            paths = re.search(r"(?m)^          path: \|\n((?:            [^\n]+\n)+)", block)[1].splitlines()
            self.assertEqual([path.strip() for path in paths], [
                "${{ runner.temp }}/kernel-native-cache/kernel-linux-sandbox/repository_cache",
                "${{ runner.temp }}/kernel-native-cache/kernel-linux-sandbox/disk_cache"])
        self.assertIn("steps.cache_bound.outputs.save == 'true'", selected["Save the bounded kernel cache"])

    def test_vs_cleanup_stops_only_this_jobs_prepared_assembly_workers(self):
        workflow_steps = steps(jobs()["vs"])
        spec = self.directory / "target/bazel-image-inputs/execution-environment.json"
        spec.parent.mkdir(parents=True)
        for title, state_name, worker, unprivileged in (
                ("Stop this job's VS host-preparation worker", "bazel-host-state", "sonic-host-123-2", False),
                ("Stop this job's VS assembly worker", "bazel-unprivileged-state", "sonic-vs-bin-123-2", True)):
            step = workflow_steps[title]
            self.assertRegex(step, r"(?m)^        if: always\(\)$")
            state = self.directory / "target" / state_name
            for have_spec, have_state in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(worker=worker, spec=have_spec, state=have_state):
                    self.calls.unlink(missing_ok=True)
                    if have_spec:
                        spec.write_text("{}")
                    else:
                        spec.unlink(missing_ok=True)
                    if have_state:
                        state.mkdir(exist_ok=True)
                    elif state.exists():
                        state.rmdir()
                    result = self.execute(shell(step), GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if not (have_spec and have_state):
                        self.assertFalse(self.calls.exists())
                        continue
                    calls = self.recorded()
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["name"], "python3")
                    args = calls[0]["args"]
                    self.assertEqual(args[0], "tools/bazel/image/run.py")
                    for flag, value in (("--workspace", str(self.directory.resolve())),
                                        ("--mount-root", str(self.directory.resolve())),
                                        ("--worker-spec", str(spec.resolve())),
                                        ("--output-user-root", str(state.resolve())),
                                        ("--persistent-worker", worker), ("--worker-action", "stop")):
                        self.assertEqual(args[args.index(flag) + 1], value)
                    self.assertEqual("--unprivileged" in args, unprivileged)

    def test_make_source_validation_reuses_outputs_and_propagates_failures(self):
        workflow_steps = steps(jobs()["vs"])
        names = list(workflow_steps)
        families = (
            ("SWSS", "//dockers/docker-orchagent", "rules/docker-orchagent.mk", "artifacts/swss", False),
            ("Syncd", "//platform/vs/docker-syncd-vs", "platform/vs/docker-syncd-vs.mk", "artifacts/syncd-vs/bazel", True),
        )
        lock = self.directory / "MODULE.bazel.lock"
        lock.write_text("already-resolved Make build\n")
        for family, owner, source, artifacts, dash in families:
            name = "Validate " + family + " source packages from the Make build"
            self.assertLess(names.index("Build and verify the VS installer and both OCI container variants"), names.index(name))
            for source_exit, make_exit in ((0, 0), (19, 0), (0, 23)):
                with self.subTest(family=family, source_exit=source_exit, make_exit=make_exit):
                    self.calls.unlink(missing_ok=True)
                    result = self.execute(shell(workflow_steps[name]),
                                          SOURCE_EXIT=str(source_exit), MAKE_EXIT=str(make_exit))
                    self.assertEqual(result.returncode, source_exit or make_exit,
                                     result.stdout + result.stderr)
                    self.assertEqual(lock.read_text(), "already-resolved Make build\n")
                    calls = self.recorded()
                    make = [call for call in calls if call["name"] == "make"]
                    self.assertEqual(len(make), 1)
                    self.assertEqual(make[0]["args"][:2], ["-f", "Makefile.work"])
                    self.assertIn("BLDENV=trixie", make[0]["args"])
                    self.assertIn("sonic-slave-run", make[0]["args"])
                    values = dict(arg.split("=", 1) for arg in make[0]["args"] if "=" in arg)
                    self.assertRegex(values["SONIC_RUN_CMDS"], r"\bbash artifacts/vs/[\w-]+\.sh$")
                    runner = [call for call in calls if "tools/bazel/ci/container.py" in call["args"]]
                    self.assertEqual(len(runner), 0 if make_exit else 1)
                    if runner:
                        args = runner[0]["args"]
                        self.assertIn("--reuse-build", args)
                        for flag, value in (("--container", owner), ("--source-file", source),
                                            ("--artifacts", artifacts), ("--bazel", self.environment["BAZEL"])):
                            self.assertEqual(args[args.index(flag) + 1], value)
                        self.assertEqual("--bazel-arg=--config=syncd-dash" in args, dash)
                        self.assertEqual("--bazel-arg=--//dockers/docker-orchagent:source_config_engine=true" in args,
                                         not dash)
                        for option in ("--distdir=/bazel_cache/source downloads", "--remote_download_all",
                                       "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
                                       "--host_platform=@sonic_build_infra//platforms:x86_64_trixie",
                                       "--repository_cache=" + self.environment["BAZEL_CONTAINER_CACHE_DIR"] + "/repository_cache",
                                       "--disk_cache=" + self.environment["BAZEL_CONTAINER_CACHE_DIR"] + "/disk_cache"):
                            self.assertIn("--bazel-arg=" + option, args)
                        self.assertEqual(runner[0]["environment"],
                                         {key: self.environment[key] for key in ("BAZEL", "BAZEL_CONTAINER_ARGS")})
                    analyses = [call for call in calls if "tools/bazel/ci/bazel_commands.py" in call["args"]]
                    self.assertEqual(len(analyses), int(dash and not source_exit and not make_exit))
                    self.assertFalse(any(call["name"] == "rm" or "clean" in call["args"] for call in calls))


if __name__ == "__main__":
    unittest.main()
