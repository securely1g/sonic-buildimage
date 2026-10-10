# SONiC build-time dashboard

The dashboard publishes existing GitHub Actions measurements at
<https://securely1g.github.io/sonic-buildimage/>. It collects metadata only; it
does not launch a build, generate a package, modify a runner, or clear a cache.

## Measurements

- List every PR, including PRs without a successful full-image measurement.
- Keep every collected run attempt and job outcome. Skipped or incomplete work
  is not a zero-second build; failed builds are not faster successful builds.
- Show the explicit image-build step separately from job setup, checks, uploads,
  and the complete job duration. Parallel job times must not be added together
  to claim elapsed workflow time.
- Keep the Make VS and Bazel installer paths separate. The Make VS step builds
  `sonic-vs.img.gz` and the SWSS debug container archive together.
- Record the run's source commit separately from the current PR head. Historical
  evidence does not establish that the latest PR revision passed.

The initial collector covers `bazel-swss-oci.yml`, `bazel-oci.yml`, and
`bazel.yml`. It can report observed durations from earlier builds, but their
cache state, hardware/configuration match, and historical tested base are not
recorded. It therefore makes **no improvement or regression percentage claim**.
Controlled baseline/head benchmarking is a separate change and must preserve
the existing full-image/package execution opt-in.

See [data-contract.md](data-contract.md) for the public JSON format. Only a
bounded set of public metadata fields is retained. Raw logs, command lines,
environment variables, machine names, and build artifacts are not published.

## Automatic publication

`.github/workflows/build-dashboard.yml` validates PR changes with read-only
permissions. Once on `master`, it collects on completion of the selected
workflows, every 15 minutes at minutes 7/22/37/52, and on manual dispatch.
GitHub schedules can be delayed; `generated_at` shows the actual refresh time.
The collector checks the complete selected workflow history, so a coalesced
completion event is recovered by the next collection.

Completed attempt records are reused from the dedicated `build-metrics` branch.
New attempts, changed latest attempts, and unfinished jobs are refreshed. Old
records remain when Actions history expires. Collection failures leave the
previous history and published site intact. A normal, non-force Git commit
persists the new JSON before the static site is deployed through the official
Pages artifact/deployment actions. Publications are serialized through the end
of deployment to prevent an older run from replacing a newer site.

The publishing job always uses trusted `master` code. A `workflow_run` event
never supplies executable PR code, artifacts, scripts, or cached executables.
The GitHub token stays in the workflow and is not included in the static site.

## Local collection and preview

```sh
python3 -B -m unittest discover -s tools/ci/build_dashboard -p 'test_*.py' -v
node --check tools/ci/build_dashboard/web/app.js
mkdir -p /tmp/sonic-build-dashboard
python3 tools/ci/build_dashboard/collect.py \
  --repo securely1g/sonic-buildimage \
  --github-cli /home/dev-user/.local/bin/gh-securely1g \
  --history /tmp/sonic-build-dashboard/data.json \
  --output /tmp/sonic-build-dashboard/data.json
cp tools/ci/build_dashboard/web/* /tmp/sonic-build-dashboard/
python3 -m http.server 8000 --bind 127.0.0.1 --directory /tmp/sonic-build-dashboard
```

Use `--backfill` to re-read available completed attempts. It does not discard
older retained records. A missing history file starts an initial collection;
invalid existing history must be corrected rather than silently reset.

## Pages setup

Enable GitHub Pages with **GitHub Actions** as its build source. Restrict the
`github-pages` deployment environment to `master`; leave existing branch
protection and unrelated environment settings intact. The workflow needs
`actions: read`, `pull-requests: read`, and `contents: write` for the history
commit, followed by `pages: write` and `id-token: write` for deployment.

An initial site may be published from an operator-pushed `gh-pages` branch
before the automation PR merges. After that first publication, change the Pages
build source to GitHub Actions. The existing site stays available; automatic
event/scheduled updates begin when this workflow reaches the default branch.
Pushing `gh-pages` with a workflow's `GITHUB_TOKEN` does not trigger a legacy
Pages build, so automatic publication uses the Pages deployment action directly.
