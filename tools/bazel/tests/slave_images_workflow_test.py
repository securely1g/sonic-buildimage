"""Exercise slave publication, managed-identity login and Make's pull fallback."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
PUBLISH = ROOT / ".github/workflows/sonic-slave-images.yml"
CONSUME = ROOT / ".github/workflows/bazel-oci.yml"


def assignment(source, name):
    lines = source.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines)
                 if any(line.startswith(name + suffix) for suffix in (" =", "=", " :=")))
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return "".join(lines[start:end + 1])


class SlaveImagesWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="slave-images-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.commands = self.directory / "commands"
        self.commands.mkdir()
        self.calls = self.directory / "calls.jsonl"
        shim = "#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with pathlib.Path(os.environ["CALLS"]).open("a") as output:
    output.write(json.dumps([name, *args]) + "\n")
operation = ("showtag" if "showtag" in args else
             "sonic-slave-build" if "sonic-slave-build" in args else args[0])
if os.environ.get("FAIL_COMMAND") == name + " " + operation:
    raise SystemExit(1)
if name == "make" and operation == "showtag":
    release = next(arg.split("=", 1)[1] for arg in args if arg.startswith("BLDENV="))
    print(os.environ.get("COMPUTED_IMAGE", "sonic-slave-" + release + ":0123456789a"))
elif name == "docker" and operation == "inspect":
    raise SystemExit(0 if os.environ.get("LOCAL_PRESENT") == "1" else 1)
elif name == "docker" and operation == "pull":
    raise SystemExit(0 if os.environ.get("PULL_HIT") == "1" else 1)
elif name == "docker" and operation == "image":
    print("{}")
'''
        for name in ("make", "docker", "az", "curl", "sudo"):
            executable = self.commands / name
            executable.write_text(shim)
            executable.chmod(0o755)
        self.environment = dict(
            PATH=str(self.commands) + os.pathsep + os.defpath,
            CALLS=str(self.calls), SONIC_CI_CONTAINER_LABEL="fixture=slave-images",
            REGISTRY_SERVER="sonicdev-microsoft.azurecr.io", REGISTRY_PORT="443")
        self.publisher = yaml.safe_load(PUBLISH.read_text())["jobs"]["publish"]
        self.consumer = yaml.safe_load(CONSUME.read_text())["jobs"]["vs_container"]

    def run_shell(self, script, **environment):
        self.calls.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", "-c", script], cwd=self.directory,
            env=dict(self.environment, **environment), text=True, capture_output=True)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_publication_allows_pr_branch_pushes_and_uses_matching_profile(self):
        workflow = yaml.safe_load(PUBLISH.read_text())
        # PyYAML's YAML 1.1 parser treats the unquoted Actions "on" key as True.
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(triggers["push"]["branches"], ["master", "vs-runner"])
        self.assertNotIn("pull_request", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(
            " ".join(self.publisher["if"].split()),
            "github.ref == 'refs/heads/master' || "
            "(github.event_name == 'push' && github.ref == 'refs/heads/vs-runner')")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.publisher["runs-on"], self.consumer["runs-on"])
        self.assertNotIn("concurrency", self.publisher)
        self.assertNotIn("concurrency", self.consumer)
        for name, value in self.publisher["env"].items():
            if name != "SONIC_CI_CONTAINER_LABEL":
                self.assertEqual(value, self.consumer["env"][name], name)
        variables = yaml.safe_load(
            (ROOT / ".azure-pipelines/azure-pipelines-repd-build-variables.yml").read_text())
        self.assertEqual(
            variables["variables"]["VERSION_CONTROL_OPTIONS"],
            "SONIC_VERSION_CONTROL_COMPONENTS=" + self.publisher["env"]["SONIC_VERSION_CONTROL_COMPONENTS"])
        for job in (self.publisher, self.consumer):
            for name in ("DEFAULT_CONTAINER_REGISTRY", "SONIC_DOCKER_APT_RETRIES",
                         "SONIC_SLAVE_DEFAULT_CONTAINER_REGISTRY",
                         "SONIC_SLAVE_DOCKER_APT_RETRIES", "DOCKER_BUILD",
                         "SONIC_BUILD_RETRY_COUNT", "SONIC_BUILD_RETRY_INTERVAL"):
                self.assertNotIn(name, job["env"])
        self.assertEqual(self.consumer["env"]["ENABLE_DOCKER_BASE_PULL"], "y")
        self.assertEqual(self.consumer["env"]["SONIC_DPKG_CACHE_METHOD"], "rwcache")
        for job in (self.publisher, self.consumer):
            self.assertFalse(any(name.startswith("SONIC_SLAVE_REUSE_") for name in job["env"]))
            names = [step.get("name") for step in job["steps"]]
            login = names.index("Authenticate to the slave image registry")
            build = ("Build and publish computed slave tags" if job is self.publisher
                     else "Prepare the host-driven SONiC slave")
            self.assertLess(login, names.index(build))
            self.assertEqual(job["steps"][login]["run"],
                             'bash tools/ci/runner/azure_acr_login.sh "${REGISTRY_SERVER%.azurecr.io}"')
        scripts = "\n".join(step.get("run", "") for step in self.publisher["steps"])
        self.assertNotIn("git commit", scripts)
        self.assertNotIn("git push", scripts)
        self.assertNotIn("docker system prune", scripts)
        cleanup = self.publisher["steps"][-1]
        self.assertIn("steps.evidence.outcome == 'success'", cleanup["if"])
        self.assertIn("cleanup_vs_workspace.py", cleanup["run"])
        checkout = self.publisher["steps"][0]["with"]["path"]
        self.assertEqual(checkout, self.publisher["defaults"]["run"]["working-directory"])

    def test_publisher_builds_and_pushes_both_computed_tags(self):
        step = next(step for step in self.publisher["steps"]
                    if step.get("name") == "Build and publish computed slave tags")
        result = self.run_shell(step["run"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded()
        self.assertEqual(calls[0], ["make", "init"])
        for index, release in enumerate(("bookworm", "trixie")):
            selected = calls[1 + index * 5:6 + index * 5]
            image = "sonic-slave-" + release + ":0123456789a"
            remote = "sonicdev-microsoft.azurecr.io:443/" + image
            for command, target in zip(selected[:2], ("showtag", "sonic-slave-build")):
                self.assertEqual(command[0], "make")
                for argument in ("Makefile.work", "BLDENV=" + release, target,
                                 "PLATFORM=generic", "PLATFORM_ARCH=amd64"):
                    self.assertIn(argument, command)
            self.assertEqual(selected[2:], [
                ["docker", "image", "inspect", image],
                ["docker", "tag", image, remote],
                ["docker", "push", remote],
            ])
        self.assertEqual(len(calls), 11)

    def test_publication_stops_on_invalid_tags_and_failed_commands(self):
        script = next(step["run"] for step in self.publisher["steps"]
                      if step.get("name") == "Build and publish computed slave tags")
        for failure in ("make init", "make showtag", "make sonic-slave-build",
                        "docker image", "docker tag", "docker push"):
            with self.subTest(failure=failure):
                result = self.run_shell(script, FAIL_COMMAND=failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(any("BLDENV=trixie" in call for call in self.recorded()))
        for image in ("sonic-slave-bookworm:latest", "sonic-slave-trixie:0123456789a", ""):
            with self.subTest(image=image):
                result = self.run_shell(script, COMPUTED_IMAGE=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid computed slave image reference", result.stderr)
                self.assertFalse(any(call[0] == "docker" for call in self.recorded()))

    def test_identity_authentication_and_failures(self):
        helper = ROOT / "tools/ci/runner/azure_acr_login.sh"
        script = 'bash "' + str(helper) + '" sonicdev-microsoft'
        expected = [
            ["az", "version", "--output", "none"],
            ["az", "login", "--identity", "--output", "none"],
            ["az", "acr", "login", "--name", "sonicdev-microsoft", "--output", "none"],
        ]
        for index, failure in enumerate(("", "az version", "az login", "az acr")):
            with self.subTest(failure=failure):
                result = self.run_shell(script, FAIL_COMMAND=failure)
                self.assertEqual(result.returncode, 1 if failure else 0, result.stderr)
                count = 3 if not failure else index
                self.assertEqual(self.recorded(), expected[:count])

    def test_missing_azure_cli_installs_and_cleans_up_on_failure(self):
        helper = ROOT / "tools/ci/runner/azure_acr_login.sh"
        # Export a controlled lookup so the production helper takes its install path.
        script = (
            'command() { if [[ $1 == -v && $2 == az ]]; then return 1; fi; '
            'builtin command "$@"; }; export -f command\n'
            'bash "' + str(helper) + '" sonicdev-microsoft')
        for failure in ("", "curl --fail", "sudo -n"):
            with self.subTest(failure=failure):
                result = self.run_shell(script, FAIL_COMMAND=failure)
                self.assertEqual(result.returncode, 1 if failure else 0, result.stderr)
                calls = self.recorded()
                self.assertEqual(calls[0][0], "curl")
                self.assertIn("https://aka.ms/InstallAzureCLIDeb", calls[0])
                installer = calls[0][-1]
                self.assertFalse(Path(installer).exists())
                if failure != "curl --fail":
                    self.assertEqual(calls[1], ["sudo", "-n", "bash", installer])
                if failure:
                    self.assertFalse(any(call[0] == "az" for call in calls))
                else:
                    self.assertEqual(calls[-1][:3], ["az", "acr", "login"])

    def test_make_reuses_local_then_pulls_then_builds(self):
        source = (ROOT / "Makefile.work").read_text()
        names = ("DOCKER_SLAVE_BASE_BUILD", "DOCKER_BASE_PULL",
                 "DOCKER_SLAVE_BASE_INSPECT", "DOCKER_SLAVE_BASE_PULL_REGISTRY")
        condition = "ifeq ($(SONIC_SLAVE_REUSE_SLAVE),y)\n"
        selection = condition + source.rsplit(condition, 1)[1].split("\nendif\n", 1)[0] + "\nendif\n"
        makefile = self.directory / "fixture.mk"
        makefile.write_text(
            "SHELL := /bin/bash\n.SHELLFLAGS := -e -c\n"
            "SLAVE_BASE_IMAGE := sonic-slave-trixie\nSLAVE_BASE_TAG := 0123456789a\n"
            "SLAVE_DIR := sonic-slave-trixie\nENABLE_DOCKER_BASE_PULL := y\n"
            "SONIC_DPKG_CACHE_METHOD := rwcache\nREGISTRY_SERVER := sonicdev-microsoft.azurecr.io\n"
            "REGISTRY_PORT := 443\nPREPARE_DOCKER := echo prepared\n"
            "COLLECT_DOCKER := echo collected\n"
            + "".join(assignment(source, name) for name in names) + selection
            + "\nall:\n\t@$(SONIC_SLAVE_BASE_BUILD)\n")
        image = "sonic-slave-trixie:0123456789a"
        cases = [
            ("1", "0", "", 0, ["inspect"]),
            ("0", "1", "", 0, ["inspect", "pull", "tag"]),
            ("0", "0", "", 0, ["inspect", "pull", "build"]),
            ("0", "0", "docker build", 1, ["inspect", "pull", "build"]),
        ]
        for local, hit, failure, status, expected in cases:
            with self.subTest(local=local, hit=hit, failure=failure):
                self.calls.unlink(missing_ok=True)
                result = subprocess.run(
                    ["/usr/bin/make", "-s", "-f", str(makefile), "all"],
                    cwd=self.directory, env=dict(self.environment, LOCAL_PRESENT=local,
                                                PULL_HIT=hit, FAIL_COMMAND=failure),
                    text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, status == 0, result.stderr)
                calls = self.recorded()
                self.assertEqual([call[1] for call in calls], expected)
                if "pull" in expected:
                    self.assertIn("sonicdev-microsoft.azurecr.io:443/" + image, calls[1])
                if "build" in expected:
                    self.assertIn(image, calls[-1])

    def test_generic_and_vs_slave_tags_match_with_repository_defaults(self):
        source = (ROOT / "Makefile.work").read_text()
        registry = ("ifneq ($(DEFAULT_CONTAINER_REGISTRY),)"
                    + source.split("ifneq ($(DEFAULT_CONTAINER_REGISTRY),)", 1)[1]
                    .split("\nifeq ($(ENABLE_DOCKER_BASE_PULL),)", 1)[0])
        render = ("$(shell CONFIGURED_ARCH="
                  + source.split("$(shell CONFIGURED_ARCH=", 1)[1]
                  .split("\n\nifeq ($(CROSS_BUILD_ENVIRON), y)", 1)[0])
        prepare = assignment(source, "PREPARE_DOCKER")
        collect = assignment(source, "COLLECT_DOCKER")
        tag = assignment(source, "SLAVE_BASE_TAG_COMPUTED")
        (self.directory / "scripts").mkdir()
        slave = self.directory / "sonic-slave-trixie"
        (slave / "buildinfo/versions").mkdir(parents=True)
        (slave / "sources.list.fixture").write_text("deb fixture trixie main\n")
        (slave / "buildinfo/versions/versions-fixture").write_text("fixture==1\n")
        renderer = self.commands / "j2"
        renderer.write_text(
            "#!" + sys.executable + "\nimport os\n"
            "print('FROM ' + os.environ['DEFAULT_CONTAINER_REGISTRY'] + 'debian:fixture')\n")
        renderer.chmod(0o755)
        shim = "#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
settings = {"command": name, "registry": os.environ.get("DEFAULT_CONTAINER_REGISTRY", ""),
            "retries": os.environ.get("SONIC_DOCKER_APT_RETRIES", "")}
with pathlib.Path(os.environ["PROFILE_CALLS"]).open("a") as output:
    output.write(json.dumps(settings) + "\n")
if name == "prepare_docker_buildinfo.sh":
    dockerfile = pathlib.Path(sys.argv[2])
    dockerfile.write_text(dockerfile.read_text() + "policy=" + settings["retries"] + "\n")
'''
        for name in ("prepare_docker_buildinfo.sh", "collect_docker_version_files.sh"):
            script = self.directory / "scripts" / name
            script.write_text(shim)
            script.chmod(0o755)
        default_registry = next(
            line for line in (ROOT / "rules/config").read_text().splitlines()
            if line.startswith("DEFAULT_CONTAINER_REGISTRY ?="))
        makefile = self.directory / "profile.mk"
        makefile.write_text(
            "SHELL := /bin/bash\n.DEFAULT_GOAL := all\n"
            + default_registry + "\n"
            "SLAVE_DIR := sonic-slave-trixie\nSLAVE_BASE_IMAGE := sonic-slave-trixie\n"
            "CONFIGURED_ARCH := amd64\nBLDENV := trixie\nSONIC_BUILD_HOOKS_HASH := fixture\n"
            + registry + "\n" + render + "\n" + prepare
            + "\n$(shell $(PREPARE_DOCKER))\n" + tag
            + "\nSLAVE_BASE_TAG := $(SLAVE_BASE_TAG_COMPUTED)\n" + collect
            + "\nall:\n\t@$(COLLECT_DOCKER)\n"
            "\t@printf '%s\\n' 'tag=$(SLAVE_BASE_TAG)' "
            "'registry=$(DEFAULT_CONTAINER_REGISTRY)' 'retries=$(SONIC_DOCKER_APT_RETRIES)'\n")
        reports = {}
        profiles = [
            ("generic", {"PLATFORM": "generic"}),
            ("vs", {"PLATFORM": "vs"}),
            ("inherited", {"DEFAULT_CONTAINER_REGISTRY": "registry.example",
                           "SONIC_DOCKER_APT_RETRIES": "0"}),
        ]
        for name, environment in profiles:
            with self.subTest(profile=name):
                capture = self.directory / (name + ".jsonl")
                result = subprocess.run(
                    ["/usr/bin/make", "-s", "-f", str(makefile)],
                    cwd=self.directory,
                    env=dict(self.environment, **environment, PROFILE_CALLS=str(capture)),
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                reports[name] = dict(line.split("=", 1) for line in result.stdout.splitlines())
                settings = [json.loads(line) for line in capture.read_text().splitlines()]
                prefix = "registry.example/" if name == "inherited" else "publicmirror.azurecr.io/"
                self.assertEqual([entry["registry"] for entry in settings], [prefix, prefix])
                self.assertEqual(settings[0]["retries"], "0" if name == "inherited" else "")
        self.assertEqual(reports["generic"]["tag"], reports["vs"]["tag"])
        self.assertNotEqual(reports["generic"]["tag"], reports["inherited"]["tag"])
        self.assertEqual(reports["vs"]["registry"], "publicmirror.azurecr.io/")
        self.assertEqual(reports["vs"]["retries"], "")


if __name__ == "__main__":
    unittest.main()
