#!/usr/bin/env python3
"""Exercise exact-pair checkout planning against a small local Git remote."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import checkout_pair as pair


def git(directory, *args, input=None):
    environment = os.environ.copy()
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    return subprocess.run(["git", "-C", str(directory), *args], input=input, capture_output=True,
                          check=True, env=environment).stdout.decode().strip()


class API:
    cli = None

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = 0
        self.overrides = {}
        self.change = None

    def get(self, path):
        self.calls += 1
        if self.change:
            self.change(self.calls, self.overrides)
        if path in self.overrides:
            return copy.deepcopy(self.overrides[path])
        repository = {"full_name": pair.REPOSITORY, "owner": {"login": "securely1g"}}
        if path == "pulls/33":
            return {"state": "open", "head": {"sha": self.fixture.head, "ref": "feature", "repo": repository},
                    "base": {"sha": self.fixture.initial, "ref": "master", "repo": repository}}
        name = path.removeprefix("git/ref/heads/")
        return {"ref": "refs/heads/" + name, "object": {"type": "commit",
                "sha": git(self.fixture.remote, "rev-parse", "refs/heads/" + name)}}


class CheckoutPairTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="checkout-pair-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "master")
        self.write("common.txt", "initial\n")
        self.initial = self.commit("Initial source")
        git(self.source, "switch", "-c", "feature")
        self.write("head.txt", "head change\n")
        self.head = self.commit("Add head change")
        git(self.source, "switch", "master")
        self.write("base.txt", "current base change\n")
        self.base = self.commit("Advance base")
        git(self.source, "switch", "-c", "validator")
        for relative in pair.SCRIPTS.values():
            self.write(relative, (ROOT / relative).read_bytes())
        self.write("validator-only.txt", "must not enter tested source\n")
        self.validator = self.commit("Add validator")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "--bare", str(self.source), str(self.remote)], capture_output=True, check=True)
        git(self.remote, "update-ref", "refs/pull/33/head", self.head)
        # This deliberately stale hosted merge is never a source input.
        git(self.remote, "update-ref", "refs/pull/33/merge", self.initial)
        self.workspace = self.root / "workspace"
        subprocess.run(["git", "clone", "--depth=1", "--branch", "validator", self.remote.as_uri(), str(self.workspace)],
                       capture_output=True, check=True)
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.output = self.root / "github-output"
        self.output.touch()
        self.api = API(self)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else value.encode())

    def commit(self, message):
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", message)
        return git(self.source, "rev-parse", "HEAD")

    def inputs(self, merge=None):
        return pair.inputs("33", self.head, "master", self.base, merge)

    def plan(self, name="plan"):
        return pair.resolve(self.inputs(), self.root / name, self.api, str(self.remote))

    def prepare(self):
        planned = self.plan()
        receipt = pair.prepare(self.inputs(planned["merge_sha"]), self.workspace, self.runner_temp,
                               self.validator, self.output, self.api, str(self.remote))
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        return planned, receipt, outputs

    def staged(self, outputs):
        path = Path(outputs["artifact_collector"]).with_name("checkout_pair.py")
        spec = importlib.util.spec_from_file_location("staged_checkout_pair", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exact_merge_contains_base_and_head_without_validator_files(self):
        self.assertEqual(git(self.workspace, "rev-parse", "--is-shallow-repository"), "true")
        planned, receipt, outputs = self.prepare()
        self.assertEqual(git(self.workspace, "rev-parse", "HEAD"), planned["merge_sha"])
        self.assertEqual(receipt["identity"]["parents"], [self.base, self.head])
        self.assertEqual((self.workspace / "base.txt").read_text(), "current base change\n")
        self.assertEqual((self.workspace / "head.txt").read_text(), "head change\n")
        self.assertFalse((self.workspace / "validator-only.txt").exists())
        self.assertFalse((self.workspace / "tools/bazel/ci/checkout_pair.py").exists())
        projection = self.staged(outputs).verify_receipt(outputs["receipt"], self.workspace,
                                                        self.inputs(planned["merge_sha"]), self.validator,
                                                        outputs["receipt_sha256"])
        self.assertEqual(projection["identity"], receipt["identity"])
        self.assertEqual(outputs["verified"], "true")

    def test_merge_is_deterministic_and_rejects_a_different_plan(self):
        first = self.plan("first")
        second = self.plan("second")
        self.assertEqual((first["merge_sha"], first["tree_sha"]), (second["merge_sha"], second["tree_sha"]))
        with self.assertRaisesRegex(pair.PairError, "merge-differs-from-plan"):
            pair.resolve(self.inputs("f" * 40), self.root / "wrong-plan", self.api, str(self.remote))

    def test_rejects_partial_or_malformed_inputs(self):
        for values in [("", self.head, "master", self.base), ("0", self.head, "master", self.base),
                       ("33", "short", "master", self.base), ("33", self.head, "../master", self.base),
                       ("33", self.head, "master", "short")]:
            with self.subTest(values=values), self.assertRaises(pair.PairError):
                pair.inputs(*values)
        with self.assertRaisesRegex(pair.PairError, "invalid-merge"):
            pair.inputs("33", self.head, "master", self.base, require_merge=True)

    def test_rejects_closed_foreign_or_moved_inputs(self):
        original = self.api.get("pulls/33")
        cases = []
        closed = copy.deepcopy(original); closed["state"] = "closed"; cases.append(("pulls/33", closed))
        foreign = copy.deepcopy(original); foreign["head"]["repo"]["full_name"] = "other/sonic-buildimage"; cases.append(("pulls/33", foreign))
        moved = copy.deepcopy(original); moved["head"]["sha"] = "f" * 40; cases.append(("pulls/33", moved))
        retargeted = copy.deepcopy(original); retargeted["base"]["ref"] = "other"; cases.append(("pulls/33", retargeted))
        for name in ("feature", "master"):
            cases.append(("git/ref/heads/" + name, {"ref": "refs/heads/" + name,
                         "object": {"type": "commit", "sha": "f" * 40}}))
        for index, (path, value) in enumerate(cases):
            api = API(self); api.overrides[path] = value
            with self.subTest(path=path), self.assertRaises(pair.PairError):
                pair.resolve(self.inputs(), self.root / ("rejected-" + str(index)), api, str(self.remote))

    def test_rejects_inputs_that_move_after_fetch(self):
        def change(call, overrides):
            if call == 4:
                overrides["git/ref/heads/master"] = {"ref": "refs/heads/master",
                    "object": {"type": "commit", "sha": self.initial}}
        self.api.change = change
        with self.assertRaisesRegex(pair.PairError, "base-moved"):
            self.plan()

    def test_rejects_conflicting_merge(self):
        git(self.source, "switch", "master")
        self.write("common.txt", "base conflict\n"); self.base = self.commit("Change base line")
        git(self.source, "switch", "feature")
        self.write("common.txt", "head conflict\n"); self.head = self.commit("Change head line")
        git(self.source, "push", str(self.remote), "+master:master", "+feature:feature")
        git(self.remote, "update-ref", "refs/pull/33/head", self.head)
        with self.assertRaisesRegex(pair.PairError, "merge-conflict"):
            self.plan()

    def test_rejects_incomplete_remote_history(self):
        shallow = self.root / "shallow.git"
        subprocess.run(["git", "clone", "--bare", "--depth=1", "--no-single-branch",
                        self.remote.as_uri(), str(shallow)], capture_output=True, check=True)
        git(shallow, "update-ref", "refs/pull/33/head", self.head)
        with self.assertRaises(pair.PairError):
            pair.resolve(self.inputs(), self.root / "incomplete", self.api, str(shallow))

    def test_fast_forward_and_contained_heads_keep_two_parents(self):
        git(self.remote, "update-ref", "refs/heads/master", self.initial)
        self.base = self.initial
        fast = self.plan("fast")
        self.assertEqual(fast["parents"], [self.base, self.head])
        git(self.source, "switch", "feature")
        self.write("contained.txt", "base after head\n"); self.base = self.commit("Contain head")
        git(self.source, "push", str(self.remote), "HEAD:refs/heads/master")
        contained = self.plan("contained")
        self.assertEqual(contained["parents"], [self.base, self.head])

    def test_receipt_rejects_changed_checkout_and_validator(self):
        planned, _receipt, outputs = self.prepare()
        verifier = self.staged(outputs)
        expected = self.inputs(planned["merge_sha"])
        git(self.workspace, "checkout", "--detach", self.base)
        with self.assertRaisesRegex(verifier.PairError, "checkout-receipt-mismatch"):
            verifier.verify_receipt(outputs["receipt"], self.workspace, expected, self.validator, outputs["receipt_sha256"])
        git(self.workspace, "checkout", "--detach", planned["merge_sha"])
        collector = Path(outputs["artifact_collector"]); collector.chmod(0o600); collector.write_text("changed\n")
        with self.assertRaisesRegex(verifier.PairError, "validator-file-mismatch"):
            verifier.verify_receipt(outputs["receipt"], self.workspace, expected, self.validator, outputs["receipt_sha256"])

    def test_receipt_rejects_wrong_tree_and_duplicate_keys(self):
        planned, receipt, outputs = self.prepare()
        verifier = self.staged(outputs); path = Path(outputs["receipt"]); path.chmod(0o600)
        changed = copy.deepcopy(receipt); changed["identity"]["tree_sha"] = "f" * 40
        changed["identity_sha256"] = verifier.canonical(changed["identity"]); path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(verifier.PairError, "checkout-receipt-mismatch"):
            verifier.verify_receipt(path, self.workspace, self.inputs(planned["merge_sha"]), self.validator,
                                    verifier.digest(path.read_bytes()))
        path.write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(verifier.PairError, "duplicate-json-key"):
            verifier.verify_receipt(path, self.workspace, self.inputs(planned["merge_sha"]), self.validator,
                                    verifier.digest(path.read_bytes()))

    def test_receipt_rejects_changed_observations(self):
        planned, receipt, outputs = self.prepare()
        verifier = self.staged(outputs); path = Path(outputs["receipt"]); path.chmod(0o600)
        expected = self.inputs(planned["merge_sha"])
        changed = copy.deepcopy(receipt); changed["remote_observations"][1]["base_sha"] = self.initial
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(verifier.PairError, "checkout-receipt-digest-mismatch"):
            verifier.verify_receipt(path, self.workspace, expected, self.validator, outputs["receipt_sha256"])
        with self.assertRaisesRegex(verifier.PairError, "receipt-observation-mismatch"):
            verifier.verify_receipt(path, self.workspace, expected, self.validator, verifier.digest(path.read_bytes()))

    def test_receipt_preserves_submodule_snapshot_after_build_changes(self):
        planned, receipt, outputs = self.prepare()
        verifier = self.staged(outputs); path = Path(outputs["receipt"]); path.chmod(0o600)
        expected = self.inputs(planned["merge_sha"])
        changed = copy.deepcopy(receipt)
        changed["submodules_verified_at_checkout"] = True
        changed["submodules_at_checkout"] = {"component": self.head}
        path.write_text(json.dumps(changed))
        with mock.patch.object(verifier, "submodule_status", side_effect=AssertionError("must not inspect post-build HEADs")):
            projection = verifier.verify_receipt(path, self.workspace, expected, self.validator,
                                                 verifier.digest(path.read_bytes()))
        self.assertEqual(projection["submodule_count_at_checkout"], 1)
        self.assertTrue(projection["submodules_verified_at_checkout"])


if __name__ == "__main__":
    unittest.main()
