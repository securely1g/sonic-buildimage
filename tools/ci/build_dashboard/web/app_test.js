"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {duration, measured, safeGithubUrl, validateSnapshot} = require("./app.js");

test("unmeasured and invalid durations never look like fast builds", () => {
  for (const value of [null, undefined, "12", -1, NaN, Infinity]) assert.equal(duration(value), "—");
  assert.equal(duration(0), "0s");
  assert.equal(duration(125), "2m 5s");
  assert.equal(duration(3660), "1h 1m");
});

test("only successful recognized image jobs enter the performance chart", () => {
  const job = {kind: "make-vs", conclusion: "success", image_build_seconds: 120};
  assert.equal(measured(job), true);
  assert.equal(measured({...job, kind: "bazel-vs"}), true);
  for (const conclusion of ["failure", "skipped", "cancelled", null]) assert.equal(measured({...job, conclusion}), false);
  for (const kind of ["container", "source", "checks", "other"]) assert.equal(measured({...job, kind}), false);
  assert.equal(measured({...job, image_build_seconds: null}), false);
});

test("metadata links stay on the configured GitHub repository", () => {
  const repo = "securely1g/sonic-buildimage";
  const expected = `https://github.com/${repo}/actions/runs/123`;
  assert.equal(safeGithubUrl(expected, repo), expected);
  for (const url of ["javascript:alert(1)", "data:text/html,hi", "https://github.com.evil.test/securely1g/sonic-buildimage/", "https://evil.test/", "https://github.com/other/repository/", "https://github.com/securely1g/sonic-buildimage-other/", "https://user:secret@github.com/securely1g/sonic-buildimage/", "https://github.com:444/securely1g/sonic-buildimage/", "https://github.com/securely1g/sonic-buildimage/../../other/repository/"]) assert.equal(safeGithubUrl(url, repo), null);
});

test("unsupported or partial snapshots fail explicitly", () => {
  const snapshot = {schema_version: 1, repository: "securely1g/sonic-buildimage", pull_requests: [], runs: []};
  assert.equal(validateSnapshot(snapshot), snapshot);
  for (const malformed of [null, {...snapshot, schema_version: 2}, {...snapshot, repository: "bad<script>"}, {...snapshot, runs: [{run_id: 1}]}, {...snapshot, pull_requests: [{number: "1", title: "PR"}]}]) assert.throws(() => validateSnapshot(malformed));
});
