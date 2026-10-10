"use strict";

// Public CI metadata only. Text is rendered through DOM textContent, never HTML.
const IMAGE_KINDS = new Set(["make-vs", "bazel-vs"]);
const KIND_LABELS = {"make-vs": "Make VS", "bazel-vs": "Bazel VS installer", container: "Container", source: "Source", checks: "Checks", other: "Other"};
const PHASES = ["setup", "build", "validation", "upload", "other"];
const state = {data: null, selectedRun: null, selectedJob: null, loading: false};

function seconds(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; }
function duration(value) {
  const number = seconds(value);
  if (number === null) return "—";
  if (number < 60) return `${Math.round(number)}s`;
  const rounded = Math.round(number);
  return rounded >= 3600 ? `${Math.floor(rounded / 3600)}h ${Math.floor(rounded % 3600 / 60)}m` : `${Math.floor(rounded / 60)}m ${rounded % 60}s`;
}
function timestamp(value) { const parsed = Date.parse(value || ""); return Number.isFinite(parsed) ? parsed : null; }
function dateTime(value, short = false) {
  if (timestamp(value) === null) return "Not recorded";
  return new Date(value).toLocaleString(undefined, short ? {month: "short", day: "numeric"} : {month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"});
}
function result(item) { return item.conclusion || item.status || "unknown"; }
function resultLabel(value) { return String(value || "unknown").replaceAll("_", " "); }
function runKey(run) { return `${run.run_id}:${run.attempt}`; }
function imageJob(job) { return IMAGE_KINDS.has(job.kind); }
function measured(job) { return imageJob(job) && job.conclusion === "success" && seconds(job.image_build_seconds) !== null; }
function kindLabel(job) { return KIND_LABELS[job.kind] || "Other"; }
function architecture(job) { return job.architecture ? String(job.architecture).toUpperCase() : "Unknown architecture"; }
function runTime(run) { return timestamp(run.started_at) || timestamp(run.completed_at) || 0; }
function safeGithubUrl(value, repository) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password && url.pathname.startsWith(`/${repository}/`) ? url.href : null;
  } catch { return null; }
}
function validateSnapshot(data) {
  if (!data || data.schema_version !== 1 || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(data.repository || "") || !Array.isArray(data.runs) || !Array.isArray(data.pull_requests)) throw new Error("The published data has an unsupported format.");
  for (const run of data.runs) {
    if (!run || !Number.isSafeInteger(run.run_id) || !Number.isSafeInteger(run.attempt) || !Array.isArray(run.jobs) || !Array.isArray(run.pr_numbers) || run.jobs.some(job => !job || !Array.isArray(job.steps))) throw new Error("The published run data is incomplete.");
  }
  for (const pr of data.pull_requests) {
    if (!pr || !Number.isSafeInteger(pr.number) || typeof pr.title !== "string") throw new Error("The published pull request data is incomplete.");
  }
  return data;
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function byId(id) { return document.getElementById(id); }
function badge(value, label) {
  const allowed = new Set(["success", "failure", "timed_out", "action_required", "startup_failure", "in_progress", "queued", "requested", "waiting", "pending", "current"]);
  return el("span", `badge${allowed.has(value) ? ` ${value}` : ""}`, label || resultLabel(value));
}
function link(text, url) {
  const safe = safeGithubUrl(url, state.data.repository);
  if (!safe) return el("span", "", text);
  const anchor = el("a", "", text);
  anchor.href = safe;
  return anchor;
}
function prNumbers(run) { return run.pr_numbers.filter(number => Number.isSafeInteger(number)); }
function matchesRun(run) {
  const selectedPr = byId("pr-filter").value;
  return selectedPr === "all" || prNumbers(run).includes(Number(selectedPr));
}
function matchesJob(job) {
  const path = byId("path-filter").value;
  const arch = byId("arch-filter").value;
  return (path === "all" || job.kind === path) && (arch === "all" || (job.architecture || "unknown") === arch);
}
function records() { return state.data.runs.flatMap(run => run.jobs.map(job => ({run, job}))); }
function successfulImages() { return records().filter(item => measured(item.job)).sort((a, b) => runTime(b.run) - runTime(a.run)); }
function setSelected(run, jobId) {
  state.selectedRun = runKey(run);
  state.selectedJob = jobId || null;
  renderCoverage();
  renderRuns();
  renderDetails();
}

function renderMetrics() {
  const successes = successfulImages();
  const latest = successes[0];
  const measuredPrs = new Set(successes.flatMap(item => prNumbers(item.run)));
  const metrics = [
    ["Latest successful image step", latest ? duration(latest.job.image_build_seconds) : "—", latest ? `${kindLabel(latest.job)} · ${architecture(latest.job)}` : "Waiting for an image measurement"],
    ["Successful image measurements", successes.length, "Build steps with a successful result"],
    ["PRs with image measurements", `${state.data.pull_requests.filter(pr => measuredPrs.has(pr.number)).length} / ${state.data.pull_requests.length}`, "Includes historical PR revisions"],
    ["Workflow runs retained", state.data.runs.length, "Each retry attempt is recorded separately"],
  ];
  byId("metrics").replaceChildren(...metrics.map(([label, value, note]) => {
    const card = el("div", "metric");
    card.append(el("p", "metric-label", label), el("p", "metric-value", value), el("p", "metric-note", note));
    return card;
  }));
}
function renderFilters() {
  const select = byId("pr-filter");
  const old = select.value;
  const all = el("option", "", "All pull requests & branches");
  all.value = "all";
  select.replaceChildren(all);
  for (const pr of [...state.data.pull_requests].sort((a, b) => b.number - a.number)) {
    const option = el("option", "", `#${pr.number} · ${pr.title}`);
    option.value = String(pr.number);
    select.append(option);
  }
  select.value = [...select.options].some(option => option.value === old) ? old : "all";
}
function svgEl(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function renderTrend() {
  const all = successfulImages().filter(item => matchesRun(item.run) && matchesJob(item.job));
  const points = all.slice(0, 60).reverse();
  const container = byId("trend");
  container.replaceChildren();
  byId("trend-note").textContent = `Showing ${points.length} of ${all.length} matching successful image measurements. Select a point to inspect its job. Lines join only the same build path and architecture; they do not imply a controlled comparison.`;
  if (!points.length) { container.append(el("p", "empty", "No successful image measurements match these filters.\nFailed, skipped, and pending runs remain visible below.")); return; }
  const padding = getComputedStyle(container);
  const width = Math.max(240, Math.round(container.clientWidth - parseFloat(padding.paddingLeft) - parseFloat(padding.paddingRight)));
  const height = width < 520 ? 250 : 265;
  const left = 55, right = 40, top = 19, bottom = 41;
  const maxSeconds = Math.max(60, ...points.map(item => item.job.image_build_seconds)) * 1.14;
  const firstTime = Math.min(...points.map(item => runTime(item.run)));
  const lastTime = Math.max(...points.map(item => runTime(item.run)));
  const x = item => lastTime === firstTime ? (width + left - right) / 2 : left + (runTime(item.run) - firstTime) / (lastTime - firstTime) * (width - left - right);
  const y = item => top + (1 - item.job.image_build_seconds / maxSeconds) * (height - top - bottom);
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Successful image build times, in minutes, ordered by run start time. Each point opens run details."});
  for (let step = 0; step <= 4; step++) {
    const value = maxSeconds * step / 4;
    const position = top + (1 - step / 4) * (height - top - bottom);
    svg.append(svgEl("line", {x1: left, y1: position, x2: width - right, y2: position, stroke: "#e6edef", "stroke-dasharray": step ? "3 4" : "0"}), svgEl("text", {x: left - 11, y: position + 4, "text-anchor": "end"}, `${Math.round(value / 60)}m`));
  }
  const dateCount = lastTime === firstTime ? 1 : width < 400 ? 2 : width < 700 ? 3 : 5;
  for (let index = 0; index < dateCount; index++) {
    const fraction = dateCount === 1 ? .5 : index / (dateCount - 1);
    const tick = firstTime + (lastTime - firstTime) * fraction;
    svg.append(svgEl("text", {x: left + fraction * (width - left - right), y: height - 13, "text-anchor": "middle"}, dateTime(new Date(tick).toISOString(), true)));
  }
  const groups = new Map();
  for (const item of points) {
    const group = `${item.job.kind}:${item.job.architecture || "unknown"}`;
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(item);
  }
  for (const items of groups.values()) {
    const color = items[0].job.kind === "make-vs" ? "#007f78" : "#23548a";
    const line = svgEl("polyline", {points: items.map(item => `${x(item)},${y(item)}`).join(" "), fill: "none", stroke: color, "stroke-opacity": ".45", "stroke-width": "2", "stroke-dasharray": items[0].job.architecture === "arm64" ? "5 4" : "0"});
    svg.append(line);
  }
  for (const item of points) {
    const prs = prNumbers(item.run).map(number => `PR #${number}`).join(", ") || item.run.branch || "Branch build";
    const label = `${prs} · ${kindLabel(item.job)} · ${architecture(item.job)} · ${duration(item.job.image_build_seconds)} · ${dateTime(item.run.started_at)}`;
    const point = svgEl("g", {class: "chart-point", tabindex: "0", role: "button", "aria-label": label});
    point.append(svgEl("title", {}, label));
    point.append(svgEl("circle", {cx: x(item), cy: y(item), r: 17, fill: "transparent", "pointer-events": "all"}));
    point.append(svgEl("circle", {class: "chart-marker", cx: x(item), cy: y(item), r: 4.5, fill: item.job.kind === "make-vs" ? "#007f78" : "#23548a", stroke: "white", "stroke-width": 2, "pointer-events": "none"}));
    point.addEventListener("click", () => setSelected(item.run, item.job.job_id));
    point.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); setSelected(item.run, item.job.job_id); } });
    svg.append(point);
  }
  container.append(svg);
}

function renderCoverage() {
  const rows = [];
  const selected = state.data.runs.find(run => runKey(run) === state.selectedRun);
  for (const pr of [...state.data.pull_requests].sort((a, b) => b.number - a.number)) {
    const associated = state.data.runs.filter(run => prNumbers(run).includes(pr.number)).sort((a, b) => runTime(b) - runTime(a));
    const latestImageRun = associated.find(run => run.jobs.some(imageJob));
    const latest = latestImageRun || associated[0];
    const jobs = latestImageRun ? latestImageRun.jobs.filter(imageJob) : [];
    const row = el("tr", selected && prNumbers(selected).includes(pr.number) ? "selected" : "");
    const title = el("td");
    const button = el("button", "table-button");
    button.type = "button";
    button.append(el("span", "pr-number", `#${pr.number}`), document.createTextNode(pr.title));
    button.addEventListener("click", () => {
      byId("pr-filter").value = String(pr.number);
      if (latest) { state.selectedRun = runKey(latest); state.selectedJob = jobs[0]?.job_id || null; }
      else { state.selectedRun = null; state.selectedJob = null; }
      renderFiltered();
    });
    title.append(button, el("span", "subtext", pr.merged_at ? "Merged" : pr.draft ? "Draft" : resultLabel(pr.state)));
    const status = el("td"), timings = el("td", "nowrap");
    if (!jobs.length) { status.append(badge("unmeasured", "Unmeasured")); timings.textContent = "—"; }
    else {
      const stack = el("div", "status-stack");
      for (const job of jobs) {
        const item = el("div");
        item.append(badge(result(job)), el("span", "subtext", `${kindLabel(job)} · ${architecture(job)}`));
        stack.append(item);
        const time = el("div", "", measured(job) ? duration(job.image_build_seconds) : "—");
        time.append(el("span", "subtext", measured(job) ? "successful step" : job.conclusion === "success" ? "no step timing" : "not a successful build"));
        timings.append(time);
      }
      status.append(stack);
    }
    if (latestImageRun) {
      const current = !!pr.head_sha && latestImageRun.source_sha === pr.head_sha;
      title.append(el("span", "subtext sha", String(latestImageRun.source_sha || "unknown").slice(0, 10)), badge(current ? "current" : "historical", current ? "Current PR head" : "Different revision"));
      title.append(el("span", "subtext", dateTime(latestImageRun.started_at)));
    }
    row.append(title, status, timings); rows.push(row);
  }
  byId("coverage-body").replaceChildren(...rows);
  byId("coverage-count").textContent = `${rows.length} PRs`;
  if (!rows.length) { const row = el("tr"), cell = el("td", "empty", "No pull requests have been collected yet."); cell.colSpan = 3; row.append(cell); byId("coverage-body").append(row); }
}
function renderRuns() {
  const all = state.data.runs.filter(run => matchesRun(run) && (byId("path-filter").value === "all" && byId("arch-filter").value === "all" || run.jobs.some(matchesJob))).sort((a, b) => runTime(b) - runTime(a));
  const rows = all.slice(0, 100).map(run => {
    const row = el("tr", state.selectedRun === runKey(run) ? "selected" : "");
    const workflow = el("td");
    const button = el("button", "table-button", run.workflow_name || run.workflow_path || "Workflow");
    button.type = "button";
    button.addEventListener("click", () => setSelected(run));
    workflow.append(button, el("span", "subtext sha", `${String(run.source_sha || "unknown").slice(0, 10)} · ${run.branch || "unknown branch"}`));
    const prs = el("td", "", prNumbers(run).map(number => `#${number}`).join(", ") || "—");
    const status = el("td"); status.append(badge(result(run)));
    row.append(workflow, prs, status, el("td", "", dateTime(run.started_at)), el("td", "", run.attempt));
    return row;
  });
  byId("runs-body").replaceChildren(...rows);
  byId("run-count").textContent = `${rows.length} of ${all.length}`;
  if (!rows.length) { const row = el("tr"), cell = el("td", "empty", "No workflow runs match these filters."); cell.colSpan = 5; row.append(cell); byId("runs-body").append(row); }
}

function renderDetails() {
  const box = byId("details"); box.replaceChildren();
  const run = state.data.runs.find(item => runKey(item) === state.selectedRun);
  if (!run) { box.append(el("p", "empty", "Select a chart point, pull request, or workflow run to see its timing breakdown.")); return; }
  const heading = el("p", "detail-meta");
  heading.append(link(`${run.workflow_name || "Workflow"} ↗`, run.url), badge(result(run)), el("span", "subtext", `${dateTime(run.started_at)} · Attempt ${run.attempt}`), el("span", "subtext sha", `Source ${String(run.source_sha || "unknown").slice(0, 12)} · ${run.event || "unknown event"}`));
  box.append(heading);
  if (run.pr_association_note) box.append(el("p", "source-warning", run.pr_association_note));
  const selectedPr = Number(byId("pr-filter").value);
  const pr = state.data.pull_requests.find(item => item.number === selectedPr) || state.data.pull_requests.find(item => prNumbers(run).includes(item.number));
  if (pr) {
    const note = el("p", "detail-meta");
    note.append(link(`PR #${pr.number}`, pr.url), document.createTextNode(` · association: ${run.pr_association || "unknown"}`));
    box.append(note);
    if (pr.head_sha !== run.source_sha) box.append(el("p", "source-warning", "The recorded source revision differs from the current PR head. This may be a historical revision or a merge build; it is not proof that the current PR head was measured."));
  }
  if (!run.jobs.length) { box.append(el("p", "empty", "No job details are available for this workflow yet.")); return; }
  const label = el("label", "detail-select", "Job");
  const select = el("select");
  for (const job of run.jobs) {
    const option = el("option", "", `${job.name} · ${resultLabel(result(job))}`);
    option.value = String(job.job_id); select.append(option);
  }
  const job = run.jobs.find(item => String(item.job_id) === String(state.selectedJob)) || run.jobs.find(imageJob) || run.jobs[0];
  select.value = String(job.job_id);
  select.addEventListener("change", () => { state.selectedJob = select.value; renderDetails(); });
  label.append(select); box.append(label);
  const meta = el("p", "detail-meta");
  meta.append(link(`${kindLabel(job)} · ${architecture(job)} ↗`, job.url), badge(result(job)));
  box.append(meta);
  const values = el("div", "detail-values");
  for (const [name, value] of [["Successful image build step", measured(job) ? job.image_build_seconds : null], ["Total job elapsed", job.duration_seconds]]) {
    const valueBox = el("div", "detail-value"); valueBox.append(el("span", "", name), el("strong", "", duration(value))); values.append(valueBox);
  }
  box.append(values);
  const phases = job.phases || {};
  const total = PHASES.reduce((sum, phase) => sum + (seconds(phases[phase]) || 0), 0);
  const bar = el("div", "stage-bar"); bar.setAttribute("aria-label", "Recorded step time by phase");
  const legend = el("div", "stage-legend");
  for (const phase of PHASES) {
    const value = seconds(phases[phase]);
    if (total > 0 && value !== null) { const segment = el("span", `stage-${phase}`); segment.style.width = `${value / total * 100}%`; segment.title = `${phase}: ${duration(value)}`; bar.append(segment); }
    const item = el("span"); item.append(el("i", `stage-${phase}`), document.createTextNode(phase[0].toUpperCase() + phase.slice(1)), el("b", "", duration(value))); legend.append(item);
  }
  box.append(bar, legend, el("p", "stage-note", "Stage totals sum recorded step durations. They may not equal job elapsed time, and the build phase can include more work than the image build step. Queue time is excluded. Cache state and runner comparability are unknown."));
  const steps = el("details", "steps"); steps.append(el("summary", "", `Step timings · ${job.steps.length} steps`));
  const list = el("ol");
  for (const step of job.steps) {
    const item = el("li"); const name = el("span", "", step.name || "Unnamed step"); const timing = el("span");
    timing.append(badge(result(step)), document.createTextNode(duration(step.duration_seconds)));
    item.append(name, timing); list.append(item);
  }
  steps.append(list); box.append(steps);
}
function renderFiltered() { renderTrend(); renderCoverage(); renderRuns(); renderDetails(); }
function render() {
  byId("dashboard").hidden = false;
  byId("snapshot-time").textContent = `Snapshot: ${dateTime(state.data.generated_at)}`;
  byId("repository-link").href = `https://github.com/${state.data.repository}`;
  renderFilters(); renderMetrics();
  if (!state.selectedRun) { const latest = successfulImages()[0]; const run = latest?.run || [...state.data.runs].sort((a, b) => runTime(b) - runTime(a))[0]; state.selectedRun = run ? runKey(run) : null; state.selectedJob = latest?.job.job_id || null; }
  renderFiltered();
}
async function refresh() {
  if (state.loading) return;
  state.loading = true; byId("refresh").disabled = true;
  const status = byId("load-status"); status.className = ""; status.textContent = state.data ? "Checking for a newer published snapshot…" : "Loading CI history…";
  try {
    const url = new URL("data.json", window.location.href); url.searchParams.set("t", String(Date.now()));
    const response = await fetch(url, {cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(30000)});
    if (!response.ok) throw new Error(`Snapshot request failed (HTTP ${response.status}).`);
    state.data = validateSnapshot(await response.json());
    render(); status.textContent = "";
  } catch (error) {
    status.className = "error";
    status.textContent = `${state.data ? "Showing the last loaded snapshot. " : "The dashboard could not load its data. "}${error.message} Try Refresh data again.`;
  } finally { state.loading = false; byId("refresh").disabled = false; }
}
if (typeof document !== "undefined") {
  byId("refresh").addEventListener("click", refresh);
  for (const id of ["path-filter", "arch-filter", "pr-filter"]) byId(id).addEventListener("change", () => { if (state.data) renderFiltered(); });
  let chartWidth = 0;
  const chartResize = new ResizeObserver(entries => {
    const width = Math.round(entries[0].contentRect.width);
    if (width > 0 && width !== chartWidth) {
      chartWidth = width;
      if (state.data) requestAnimationFrame(renderTrend);
    }
  });
  chartResize.observe(byId("trend"));
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 300000);
}
if (typeof module !== "undefined") module.exports = {seconds, duration, timestamp, safeGithubUrl, validateSnapshot, measured};
