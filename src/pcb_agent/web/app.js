"use strict";

const state = {
  csrf: "",
  diagnostics: null,
  projects: [],
  current: null,
  activeJob: null,
  eventSource: null,
  eventCursor: 0,
  pollTimer: null,
};

const byId = (id) => document.getElementById(id);
const node = (tag, className, text) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = String(text);
  return value;
};
const clear = (element) => { while (element.firstChild) element.firstChild.remove(); };

async function api(path, options = {}) {
  const request = { method: options.method || "GET", headers: {} };
  if (request.method !== "GET") {
    request.headers["Content-Type"] = "application/json";
    request.headers["X-CopperWright-CSRF"] = state.csrf;
    request.body = JSON.stringify(options.body || {});
  }
  const response = await fetch(path, request);
  const content = await response.json();
  if (!response.ok) throw new Error(content.error?.message || `Request failed (${response.status})`);
  return content;
}

function toast(message, error = false) {
  const target = byId("toast");
  target.textContent = message;
  target.classList.toggle("error", error);
  target.classList.remove("hidden");
  window.setTimeout(() => target.classList.add("hidden"), 4200);
}

function statusTone(status) {
  if (["validated", "released"].includes(status)) return "good";
  if (["unsupported", "validation_failed", "generation_failed", "release_failed", "provider_error"].includes(status)) return "bad";
  return "warn";
}

function renderProjectList() {
  const list = byId("project-list");
  clear(list);
  if (!state.projects.length) {
    list.append(node("p", "loading-line", "No saved projects yet."));
    return;
  }
  for (const project of state.projects) {
    const button = node("button", `project-button${state.current?.project.id === project.id ? " active" : ""}`);
    button.type = "button";
    button.dataset.projectId = project.id;
    const dot = node("span", `dot ${statusTone(project.status)}`);
    dot.setAttribute("aria-hidden", "true");
    const copy = node("span");
    copy.append(node("strong", "", project.name), node("span", "", project.status.replaceAll("_", " ")));
    button.append(dot, copy);
    button.addEventListener("click", () => selectProject(project.id));
    list.append(button);
  }
}

function renderDiagnostics() {
  const target = byId("diagnostics");
  clear(target);
  target.append(node("h2", "", "First-run checks"));
  const provider = state.diagnostics.provider;
  const tableReady = Object.values(state.diagnostics.kicad_library_tables).every((item) => item.configured);
  const checks = [
    ["Provider", `${provider.id} · ${provider.available ? "ready" : "setup needed"}`, provider.available],
    ["KiCad CLI", state.diagnostics.tools["kicad-cli"]?.available ? "ready" : "missing", state.diagnostics.tools["kicad-cli"]?.available],
    ["KiCad libraries", tableReady ? "ready" : "setup needed", tableReady],
    ["Workspace", "private local storage", true],
  ];
  for (const [label, value, ok] of checks) {
    const row = node("div", "diagnostic-row");
    row.append(node("span", "", label), node("strong", ok ? "ok" : "missing", value));
    target.append(row);
  }
  const detail = node("p", "loading-line", state.diagnostics.credential_guidance.persistence);
  target.append(detail);
  byId("active-provider").textContent = `Active provider: ${provider.id}. Provider selection is fixed for this server process; restart CopperWright to change it.`;
}

function renderMessages(view) {
  const target = byId("messages");
  clear(target);
  const messages = view.conversation.messages;
  if (!messages.length) {
    target.append(node("p", "loading-line", "Describe the board to begin."));
    return;
  }
  for (const message of messages) {
    const wrapper = node("article", `message ${message.role} ${message.kind}`);
    const avatar = node("div", "avatar", message.role === "user" ? "You" : "CW");
    avatar.setAttribute("aria-hidden", "true");
    const body = node("div", "message-body");
    body.append(node("p", "", message.text));
    body.append(node("div", "message-meta", `${message.kind.replaceAll("_", " ")} · ${message.created_at}`));
    wrapper.append(avatar, body);
    target.append(wrapper);
  }
  const clarifications = view.conversation.proposal?.clarifications || [];
  if (clarifications.length) {
    const choices = node("div", "scope-chips");
    choices.setAttribute("aria-label", "Clarification choices");
    for (const choice of clarifications[0].choices || []) {
      const button = node("button", "secondary", choice);
      button.type = "button";
      button.addEventListener("click", () => sendMessage(choice));
      choices.append(button);
    }
    target.append(choices);
  }
  target.scrollTop = target.scrollHeight;
}

function card(title) {
  const result = node("section", "panel-card");
  result.append(node("h2", "", title));
  return result;
}

function renderBrief(view) {
  const target = byId("tab-brief");
  clear(target);
  const proposal = view.conversation.proposal;
  if (!proposal) {
    const waiting = card("Design brief");
    waiting.append(node("p", "", "Your request, assumptions, parts, and constraints will appear here before generation."));
    target.append(waiting);
    return;
  }
  const scope = card("Scope decision");
  const scopeMetric = node("div", "metric-grid");
  const decision = node("div", "metric");
  decision.append(node("strong", proposal.scope.decision === "supported" ? "Supported" : "Rejected"), node("span", "", "bounded v1 decision"));
  const profile = node("div", "metric");
  profile.append(node("strong", "", proposal.proposed_profile), node("span", "", "verified profile"));
  scopeMetric.append(decision, profile);
  scope.append(scopeMetric);
  const reasons = node("ul");
  for (const reason of proposal.scope.reasons || []) reasons.append(node("li", "", reason));
  scope.append(reasons);
  target.append(scope);
  if (!proposal.brief) return;

  const brief = proposal.brief;
  const summary = card("Human-readable brief");
  summary.append(node("p", "", brief.purpose));
  const board = node("div", "metric-grid");
  const boardSize = node("div", "metric");
  boardSize.append(node("strong", "", `${brief.board.width_mm} × ${brief.board.height_mm} mm`), node("span", "", "board envelope"));
  const layers = node("div", "metric");
  layers.append(node("strong", "", `${brief.board.layers} layers`), node("span", "", "copper stack"));
  board.append(boardSize, layers);
  summary.append(board);
  target.append(summary);

  const assumptions = card("Assumptions & external gates");
  const assumptionList = node("ul");
  for (const item of brief.assumptions) assumptionList.append(node("li", "", item));
  for (const item of proposal.scope.external_gates) assumptionList.append(node("li", "", `${item} — external`));
  assumptions.append(assumptionList);
  target.append(assumptions);

  const bom = card("Parts / BOM");
  const bomWrap = node("div", "table-wrap");
  const table = node("table");
  const header = node("tr");
  for (const label of ["Refs", "Value", "Part contract", "Qty"]) header.append(node("th", "", label));
  const head = node("thead"); head.append(header); table.append(head);
  const body = node("tbody");
  for (const item of brief.bom) {
    const row = node("tr");
    row.append(node("td", "", item.references.join(", ")), node("td", "", item.value), node("td", "", item.part_id), node("td", "", item.quantity));
    body.append(row);
  }
  table.append(body); bomWrap.append(table); bom.append(bomWrap); target.append(bom);

  const constraints = card(`Constraints (${brief.constraints.length})`);
  const list = node("ul");
  for (const item of brief.constraints) list.append(node("li", "", `${item.id} · ${item.kind} · ${item.severity}`));
  constraints.append(list); target.append(constraints);

  if (view.active_change) {
    const change = card("Pending semantic change");
    change.append(node("p", "", view.active_change.request));
    const diff = view.active_change.diff;
    change.append(node("div", "path", `${diff.before_hash.slice(0, 12)} → ${diff.after_hash.slice(0, 12)}`));
    const changed = node("ul");
    for (const [field, values] of Object.entries(diff.board_fields || {})) changed.append(node("li", "", `${field}: ${JSON.stringify(values.before)} → ${JSON.stringify(values.after)}`));
    change.append(changed); target.prepend(change);
  }
}

function artifactLink(projectId, key, text) {
  const link = node("a", "", text);
  link.href = `/api/projects/${projectId}/artifact/${key}`;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function renderArtifacts(view) {
  const target = byId("tab-artifacts");
  clear(target);
  const projectId = view.project.id;
  const preview = view.artifacts.previews;
  if (!preview) {
    const empty = card("Visual evidence");
    empty.append(node("p", "", view.design ? "Preview export has not completed. Retry the preview action." : "Confirm generation to produce real KiCad previews."));
    target.append(empty);
    return;
  }
  const board = card("PCB 3D render");
  const boardImage = node("img", "preview");
  boardImage.src = `/api/projects/${projectId}/artifact/board_render`;
  boardImage.alt = "Top-side KiCad 3D render of the generated board";
  board.append(boardImage); target.append(board);
  const schematic = card("Schematic preview");
  const schematicImage = node("img", "preview");
  schematicImage.src = `/api/projects/${projectId}/artifact/schematic_svg`;
  schematicImage.alt = "KiCad schematic preview";
  schematic.append(schematicImage); target.append(schematic);
  const links = card("Artifacts & source");
  const linkRow = node("div", "artifact-links");
  for (const [key, label] of [["schematic_pdf", "Schematic PDF"], ["board_svg", "Board SVG"], ["schematic", ".kicad_sch"], ["board", ".kicad_pcb"], ["kicad_project", ".kicad_pro"], ["requirements", "Requirements JSON"], ["ir", "Semantic IR"]]) linkRow.append(artifactLink(projectId, key, label));
  if (view.artifacts.release) linkRow.append(artifactLink(projectId, "release_archive", "Release ZIP"));
  links.append(linkRow);
  if (view.design) links.append(node("div", "path", view.design.root));
  target.append(links);
}

function renderValidation(view) {
  const target = byId("tab-validation");
  clear(target);
  const validation = view.artifacts.validation;
  if (!validation) {
    const empty = card("L0–L7 validation");
    empty.append(node("p", "", "No validation evidence yet. Generation runs the applicable real KiCad and CopperWright gates."));
    target.append(empty);
    return;
  }
  const readiness = card("Readiness claim");
  const metrics = node("div", "metric-grid");
  const candidate = node("div", "metric");
  candidate.append(node("strong", "", validation.candidate_ready ? "Candidate ready" : "Blocked"), node("span", "", "engineering-candidate gate"));
  const production = node("div", "metric");
  production.append(node("strong", "", "Not production-signed"), node("span", "", "human + physical gates remain"));
  metrics.append(candidate, production); readiness.append(metrics); target.append(readiness);
  const levels = card("Layered results");
  for (const level of validation.levels || []) {
    const row = node("div", "level-row");
    row.append(node("span", "level-name", level.level));
    const copy = node("div");
    copy.append(node("strong", "", level.name.replaceAll("_", " ")), node("span", "", level.state.replaceAll("_", " ")));
    const tone = level.outcome === "pass" ? "state-pass" : level.outcome === "fail" ? "state-fail" : "state-warn";
    row.append(copy, node("span", tone, level.outcome));
    levels.append(row);
  }
  target.append(levels);
  const noteworthy = [];
  for (const level of validation.levels || []) {
    for (const check of level.checks || []) {
      if (check.outcome !== "pass" || !["completed", "not_applicable"].includes(check.state)) {
        noteworthy.push({ level: level.level, ...check });
      }
    }
  }
  if (noteworthy.length) {
    const findings = card("Actionable findings & honest external gates");
    for (const finding of noteworthy) {
      const item = node("article", `finding ${finding.outcome === "fail" ? "finding-fail" : "finding-external"}`);
      const heading = node("div", "finding-heading");
      heading.append(
        node("strong", "", `${finding.level} · ${finding.id}`),
        node("span", finding.outcome === "fail" ? "state-fail" : "state-warn", `${finding.state.replaceAll("_", " ")} · ${finding.outcome}`),
      );
      item.append(heading, node("p", "", finding.summary));
      findings.append(item);
    }
    target.append(findings);
  }
  const actions = card("Actions");
  const row = node("div", "artifact-links");
  const validate = node("button", "secondary", "Run validation again");
  validate.type = "button"; validate.addEventListener("click", () => runAction("validate"));
  const undo = node("button", "secondary", "Undo last change");
  undo.type = "button"; undo.disabled = !view.state.last_transaction; undo.addEventListener("click", () => runAction("undo"));
  row.append(validate, undo);
  const report = artifactLink(view.project.id, "validation_report", "Open full validation JSON");
  row.append(report);
  actions.append(row); target.append(actions);
}

function renderConfirmation(view) {
  const panel = byId("confirmation");
  const confirm = byId("confirm");
  const discard = byId("discard");
  const status = view.project.status;
  if (status === "awaiting_confirmation") {
    panel.classList.remove("hidden"); discard.classList.add("hidden");
    byId("confirmation-title").textContent = "Confirm generation";
    byId("confirmation-copy").textContent = "Create native KiCad files from this reviewed semantic intent?";
    confirm.textContent = "Generate & validate";
  } else if (status === "change_ready") {
    panel.classList.remove("hidden"); discard.classList.remove("hidden");
    byId("confirmation-title").textContent = "Confirm semantic change";
    byId("confirmation-copy").textContent = "The staged diff passed validation; current files remain unchanged.";
    confirm.textContent = "Apply safely";
  } else {
    panel.classList.add("hidden");
  }
}

function renderProject(view) {
  state.current = view;
  byId("empty-state").classList.add("hidden");
  byId("project-workspace").classList.remove("hidden");
  byId("project-eyebrow").textContent = `${view.project.status.replaceAll("_", " ")} · revision ${view.project.design_revision}`;
  byId("project-title").textContent = view.project.name;
  byId("open-kicad").disabled = !view.design;
  byId("release").disabled = !view.artifacts.validation?.candidate_ready || Boolean(activeJob(view));
  byId("message-input").disabled = Boolean(activeJob(view));
  byId("send-message").disabled = Boolean(activeJob(view));
  renderMessages(view);
  renderBrief(view);
  renderArtifacts(view);
  renderValidation(view);
  renderConfirmation(view);
  renderProjectList();
  renderJob(view);
  window.localStorage.setItem("copperwright-project", view.project.id);
}

function activeJob(view = state.current) {
  return view?.jobs?.find((job) => ["queued", "running", "cancel_requested"].includes(job.status)) || null;
}

function renderJob(view = state.current) {
  const job = activeJob(view) || state.activeJob;
  const panel = byId("job-panel");
  if (!job || ["completed", "completed_after_cancel", "failed", "cancelled", "interrupted"].includes(job.status) && job !== state.activeJob) {
    panel.classList.add("hidden"); return;
  }
  state.activeJob = job;
  panel.classList.remove("hidden");
  byId("job-title").textContent = `${job.action.replaceAll("_", " ")} · ${job.status.replaceAll("_", " ")}`;
  byId("job-detail").textContent = job.error || "Progress is persisted; you can safely reopen this project.";
  const terminal = ["completed", "completed_after_cancel", "failed", "cancelled", "interrupted"].includes(job.status);
  byId("cancel-job").classList.toggle("hidden", terminal || job.status === "cancel_requested");
  byId("retry-job").classList.toggle("hidden", !["failed", "cancelled", "interrupted"].includes(job.status));
  panel.querySelector(".spinner").classList.toggle("hidden", terminal);
}

async function refreshProjects() {
  const bootstrap = await api("/api/bootstrap");
  state.csrf = bootstrap.csrf_token;
  state.diagnostics = bootstrap.diagnostics;
  state.projects = bootstrap.projects;
  renderDiagnostics(); renderProjectList();
}

async function selectProject(projectId) {
  try {
    const view = await api(`/api/projects/${projectId}`);
    renderProject(view);
    connectEvents(projectId);
    if (activeJob(view)) pollJob(projectId, activeJob(view).id);
  } catch (error) { toast(error.message, true); }
}

function connectEvents(projectId) {
  if (state.eventSource) state.eventSource.close();
  state.eventCursor = 0;
  const source = new EventSource(`/api/projects/${projectId}/events?after=0`);
  state.eventSource = source;
  source.addEventListener("progress", (event) => {
    const value = JSON.parse(event.data);
    state.eventCursor = Math.max(state.eventCursor, value.sequence);
    byId("job-detail").textContent = value.message;
  });
}

async function sendMessage(text) {
  if (!state.current || !text.trim()) return;
  try {
    const result = await api(`/api/projects/${state.current.project.id}/messages`, { method: "POST", body: { text: text.trim() } });
    state.activeJob = result.job; renderJob(); pollJob(state.current.project.id, result.job.id);
  } catch (error) { toast(error.message, true); }
}

async function runAction(action) {
  if (!state.current) return;
  try {
    const result = await api(`/api/projects/${state.current.project.id}/${action}`, { method: "POST", body: {} });
    state.activeJob = result.job; renderJob(); pollJob(state.current.project.id, result.job.id);
  } catch (error) { toast(error.message, true); }
}

function pollJob(projectId, jobId) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  const poll = async () => {
    try {
      const view = await api(`/api/projects/${projectId}`);
      const job = view.jobs.find((item) => item.id === jobId);
      state.activeJob = job || null;
      renderProject(view);
      if (job && ["queued", "running", "cancel_requested"].includes(job.status)) {
        state.pollTimer = window.setTimeout(poll, 650);
      } else {
        await refreshProjects();
        if (job?.status === "failed") toast(job.error || "Job failed", true);
        else if (job) toast(`${job.action.replaceAll("_", " ")} ${job.status.replaceAll("_", " ")}`);
      }
    } catch (error) { toast(error.message, true); }
  };
  state.pollTimer = window.setTimeout(poll, 250);
}

async function cancelActiveJob() {
  if (!state.current || !state.activeJob) return;
  try {
    const result = await api(`/api/projects/${state.current.project.id}/jobs/${state.activeJob.id}/cancel`, { method: "POST", body: {} });
    state.activeJob = result.job; renderJob();
  } catch (error) { toast(error.message, true); }
}

async function retryActiveJob() {
  if (!state.current || !state.activeJob) return;
  try {
    const result = await api(`/api/projects/${state.current.project.id}/jobs/${state.activeJob.id}/retry`, { method: "POST", body: {} });
    state.activeJob = result.job; renderJob(); pollJob(state.current.project.id, result.job.id);
  } catch (error) { toast(error.message, true); }
}

function bindEvents() {
  const dialog = byId("new-project-dialog");
  const setupDialog = byId("setup-dialog");
  for (const id of ["new-project", "empty-new-project"]) byId(id).addEventListener("click", () => dialog.showModal());
  byId("close-new-project").addEventListener("click", () => dialog.close());
  byId("provider-setup").addEventListener("click", () => setupDialog.showModal());
  byId("close-setup").addEventListener("click", () => setupDialog.close());
  byId("new-project-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = byId("new-project-name").value.trim();
    const request = byId("new-project-request").value.trim();
    if (!name || !request) return;
    try {
      const result = await api("/api/projects", { method: "POST", body: { name, request } });
      dialog.close(); byId("new-project-form").reset();
      state.current = result.project; state.activeJob = result.job;
      renderProject(result.project); connectEvents(result.project.project.id);
      pollJob(result.project.project.id, result.job.id);
      await refreshProjects();
    } catch (error) { toast(error.message, true); }
  });
  byId("composer").addEventListener("submit", (event) => {
    event.preventDefault(); const input = byId("message-input"); const text = input.value; input.value = ""; sendMessage(text);
  });
  byId("confirm").addEventListener("click", () => runAction(state.current?.project.status === "change_ready" ? "apply-change" : "confirm"));
  byId("discard").addEventListener("click", () => runAction("discard-change"));
  byId("release").addEventListener("click", () => runAction("release"));
  byId("open-kicad").addEventListener("click", async () => {
    try { const result = await api(`/api/projects/${state.current.project.id}/open-kicad`, { method: "POST", body: {} }); toast(`Opened ${result.path}`); }
    catch (error) { toast(error.message, true); }
  });
  byId("cancel-job").addEventListener("click", cancelActiveJob);
  byId("retry-job").addEventListener("click", retryActiveJob);
  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => {
    for (const item of document.querySelectorAll(".tab")) { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", item === tab ? "true" : "false"); }
    for (const panel of document.querySelectorAll(".tab-panel")) panel.classList.toggle("hidden", panel.id !== `tab-${tab.dataset.tab}`);
  });
}

async function start() {
  bindEvents();
  try {
    await refreshProjects();
    const remembered = window.localStorage.getItem("copperwright-project");
    const initial = state.projects.find((item) => item.id === remembered) || state.projects[0];
    if (initial) await selectProject(initial.id);
  } catch (error) {
    byId("diagnostics").textContent = "First-run diagnostics failed.";
    toast(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", start);
