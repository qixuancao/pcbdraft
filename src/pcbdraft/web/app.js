"use strict";

const state = {
  csrf: "",
  session: "",
  diagnostics: null,
  projects: [],
  current: null,
  activeJob: null,
  eventSource: null,
  eventCursor: 0,
  pollTimer: null,
};

const TOOL_PRESENTATION = {
  pcb_plan_request: "Understanding the board request",
  pcb_generate_candidate: "Generating the KiCad project",
  pcb_validate: "Checking the PCB candidate",
  pcb_repair_candidate: "Repairing the PCB candidate",
  pcb_apply_candidate: "Applying the checked PCB change",
  pcb_discard_candidate: "Discarding the staged PCB change",
  pcb_undo_last_change: "Restoring the previous PCB design",
  pcb_render_previews: "Rendering board previews",
  pcb_build_release: "Building release evidence",
};

const ACTIVE_TOOL_STATES = new Set(["proposed", "waiting_approval", "running"]);
const RETRYABLE_JOB_STATES = new Set(["failed", "cancelled", "interrupted"]);

const byId = (id) => document.getElementById(id);
const node = (tag, className, text) => {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = String(text);
  return value;
};
const clear = (element) => { while (element.firstChild) element.firstChild.remove(); };

async function api(path, options = {}) {
  const request = {
    method: options.method || "GET",
    headers: { "X-PCBDraft-Session": state.session },
  };
  if (request.method !== "GET") {
    request.headers["Content-Type"] = "application/json";
    request.headers["X-PCBDraft-CSRF"] = state.csrf;
    request.body = JSON.stringify(options.body || {});
  }
  const response = await fetch(path, request);
  const content = await response.json();
  if (!response.ok) throw new Error(content.error?.message || `Request failed (${response.status})`);
  return content;
}

function sessionUrl(path) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("session", state.session);
  return `${url.pathname}${url.search}`;
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
  if (["generation_unavailable", "validation_failed", "generation_failed", "release_failed", "provider_error"].includes(status)) return "bad";
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
  const orchestration = state.diagnostics.agent_orchestration;
  const tableReady = Object.values(state.diagnostics.kicad_library_tables).every((item) => item.configured);
  const dataReady = Object.values(state.diagnostics.kicad_library_data).every((item) => item.available);
  const checks = [
    ["Provider", `${provider.id} · ${provider.available ? "ready" : "setup needed"}`, provider.available],
    ["Agent router", orchestration.router.replaceAll("-", " "), true],
    ["KiCad CLI", state.diagnostics.tools["kicad-cli"]?.available ? "ready" : "missing", state.diagnostics.tools["kicad-cli"]?.available],
    ["KiCad libraries", tableReady && dataReady ? "ready" : "setup needed", tableReady && dataReady],
    ["Workspace", "private local storage", true],
  ];
  for (const [label, value, ok] of checks) {
    const row = node("div", "diagnostic-row");
    row.append(node("span", "", label), node("strong", ok ? "ok" : "missing", value));
    target.append(row);
  }
  const detail = node("p", "loading-line", state.diagnostics.credential_guidance.persistence);
  target.append(detail);
  byId("active-provider").textContent = `Active provider: ${provider.id}. Provider selection is fixed for this server process; restart PCBDraft to change it.`;
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

function toolLabel(toolName) {
  return TOOL_PRESENTATION[toolName] || toolName.replaceAll("_", " ");
}

function statusLabel(status) {
  return String(status || "unknown").replaceAll("_", " ");
}

function statusToneClass(status) {
  if (status === "completed") return "activity-good";
  if (["failed", "interrupted"].includes(status)) return "activity-bad";
  if (["denied", "cancelled"].includes(status)) return "activity-muted";
  return "activity-live";
}

function toolDetail(tool) {
  if (tool.error) return tool.error;
  if (tool.after_status) {
    return `${statusLabel(tool.before_status)} → ${statusLabel(tool.after_status)} · state revision ${tool.before_revision} → ${tool.after_revision}`;
  }
  if (tool.status === "waiting_approval") return `Paused before dispatch at state revision ${tool.baseline_revision}`;
  if (tool.status === "running") return `Running against state revision ${tool.baseline_revision}`;
  return `Bound to state revision ${tool.baseline_revision}`;
}

function renderToolRun(tool) {
  const details = node("details", `tool-run ${statusToneClass(tool.status)}`);
  details.open = ACTIVE_TOOL_STATES.has(tool.status) || ["failed", "interrupted"].includes(tool.status);
  const summary = node("summary", "tool-summary");
  const failed = ["failed", "interrupted"].includes(tool.status);
  const marker = node("span", "tool-marker", tool.status === "completed" ? "✓" : failed ? "×" : tool.status === "running" ? "◆" : "○");
  marker.setAttribute("aria-hidden", "true");
  const identity = node("span", "tool-identity");
  identity.append(node("strong", "", toolLabel(tool.tool_name)), node("code", "", tool.tool_name));
  summary.append(marker, identity, node("span", "tool-status", statusLabel(tool.status)));

  const body = node("div", "tool-body");
  body.append(node("p", "", toolDetail(tool)));
  const metadata = node("div", "tool-metadata");
  metadata.append(
    node("span", "", `source ${tool.source.replaceAll("_", " ")} · ${tool.effect.replaceAll("_", " ")} · ${tool.risk} risk`),
    node("code", "", tool.tool_call_id),
  );
  body.append(metadata);
  if (tool.arguments && Object.keys(tool.arguments).length) {
    body.append(node("pre", "tool-receipt", `Arguments\n${JSON.stringify(tool.arguments, null, 2)}`));
  }
  if (tool.result) {
    body.append(node("pre", "tool-receipt", `Result receipt\n${JSON.stringify(tool.result, null, 2)}`));
  }
  details.append(summary, body);
  return details;
}

function renderAgentActivity(view) {
  const panel = byId("agent-activity");
  const target = byId("turn-activity");
  const approval = byId("approval-readonly");
  clear(target); clear(approval);
  const agent = view.agent;
  const turns = agent?.turns || [];
  const recentTurns = turns.slice(-4);
  const pending = agent?.pending_approval || null;
  byId("agent-mode").textContent = agent
    ? `${agent.call_producer.replaceAll("-", " ")} · ${agent.permission_mode.replaceAll("_", " ")} policy`
    : "";

  for (const [index, turn] of recentTurns.entries()) {
    const wrapper = node("details", "agent-turn");
    const isLatest = index === recentTurns.length - 1;
    wrapper.open = isLatest || ["running", "waiting_approval", "failed", "interrupted"].includes(turn.status);
    const summary = node("summary", "turn-summary");
    const rawRequest = String(turn.request || "Agent turn");
    const request = rawRequest.startsWith("/pcb_") ? "Explicit PCB action" : rawRequest;
    summary.append(
      node("span", "turn-request", request),
      node("span", `turn-status ${statusToneClass(turn.status)}`, statusLabel(turn.status)),
    );
    const calls = node("div", "tool-calls");
    if (turn.tool_runs.length) {
      for (const tool of turn.tool_runs) calls.append(renderToolRun(tool));
    } else {
      calls.append(node("p", "activity-empty", "No PCB tool has been proposed for this turn yet."));
    }
    wrapper.append(summary, calls);
    target.append(wrapper);
  }

  if (pending) {
    approval.classList.remove("hidden");
    approval.append(
      node("strong", "", `Approval retained for ${pending.tool_name}`),
      node("p", "", `This exact ${pending.risk}-risk call is paused at state revision ${pending.baseline_revision}. The browser exposes it read-only; resolve it from the TUI running in review mode.`),
      node("code", "", pending.tool_call_id),
    );
  } else {
    approval.classList.add("hidden");
  }
  panel.classList.toggle("hidden", !turns.length && !pending);
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
    waiting.append(node("p", "", "Your request, assumptions, parts, and constraints will appear here as the agent works."));
    target.append(waiting);
    return;
  }
  const scope = card("Generation request");
  const scopeMetric = node("div", "metric-grid");
  const decision = node("div", "metric");
  const decisionLabel = proposal.scope.decision === "attempted"
    ? "Normal generation path"
    : "Backend unavailable";
  decision.append(node("strong", "", decisionLabel), node("span", "", "request handling"));
  const planning = node("div", "metric");
  planning.append(
    node("strong", "", proposal.planning?.state || "not started"),
    node("span", "", "circuit planning"),
  );
  scopeMetric.append(decision, planning);
  scope.append(scopeMetric);
  const warnings = node("ul");
  for (const warning of proposal.scope.warnings || []) warnings.append(node("li", "", warning));
  scope.append(warnings);
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

  const assumptions = card("Assumptions");
  const assumptionList = node("ul");
  for (const item of brief.assumptions) assumptionList.append(node("li", "", item));
  assumptions.append(assumptionList);
  target.append(assumptions);

  const identity = brief.identity;
  const requested = identity?.requested_parts || [];
  if (requested.length) {
    const identityCard = card("Requested parts");
    identityCard.append(node("p", "", `Preserved for planning: ${requested.join(", ")}`));
    target.append(identityCard);
  }

  const bom = card("Stock KiCad parts");
  const bomWrap = node("div", "table-wrap");
  const table = node("table");
  const header = node("tr");
  for (const label of ["Refs", "Value", "KiCad symbol", "Qty"]) header.append(node("th", "", label));
  const head = node("thead"); head.append(header); table.append(head);
  const body = node("tbody");
  for (const item of brief.bom) {
    const row = node("tr");
    row.append(node("td", "", item.references.join(", ")), node("td", "", item.value), node("td", "", item.symbol || ""), node("td", "", item.quantity));
    body.append(row);
  }
  table.append(body); bomWrap.append(table); bom.append(bomWrap); target.append(bom);

  const constraints = card(`Board and routing rules (${brief.constraints.length})`);
  const list = node("ul");
  for (const item of brief.constraints) list.append(node("li", "", item.kind.replaceAll("_", " ")));
  constraints.append(list); target.append(constraints);

  const planReview = brief.plan_review;
  if (planReview) {
    const count = planReview.summary?.attention_required || 0;
    const findings = (planReview.findings || []).filter((finding) => finding.outcome !== "pass");
    if (findings.length) {
      const preflight = card(`Topology warnings (${count})`);
      preflight.append(node("p", "", "Generation remains available. These checks do not certify electrical or manufacturing behavior."));
      for (const finding of findings) {
        const failed = finding.outcome === "fail";
        const item = node("article", `finding ${failed ? "finding-fail" : "finding-external"}`);
        const heading = node("div", "finding-heading");
        heading.append(
          node("strong", "", finding.id),
          node("span", failed ? "state-fail" : finding.outcome === "pass" ? "state-pass" : "state-warn", finding.outcome),
        );
        item.append(heading, node("p", "", finding.summary));
        if ((finding.evidence || []).length) item.append(node("div", "path", finding.evidence.join(" · ")));
        item.append(node("p", "", finding.action));
        preflight.append(item);
      }
      target.append(preflight);
    }
  }

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
  link.href = sessionUrl(`/api/projects/${projectId}/artifact/${key}`);
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function renderArtifacts(view) {
  const target = byId("tab-artifacts");
  clear(target);
  const projectId = view.project.id;
  const attempts = view.attempts || [];
  if (attempts.length) {
    const attemptCard = card("Generation attempts");
    for (const attempt of attempts) {
      const item = node("article", `finding ${attempt.status === "failed" ? "finding-fail" : "finding-external"}`);
      const heading = node("div", "finding-heading");
      heading.append(
        node("strong", "", attempt.status),
        node("span", attempt.status === "failed" ? "state-fail" : "state-warn", attempt.phase.replaceAll("_", " ")),
      );
      item.append(heading, node("p", "", attempt.error || "Generated work retained for inspection."));
      item.append(node("div", "path", attempt.root));
      attemptCard.append(item);
    }
    target.append(attemptCard);
  }
  const preview = view.artifacts.previews;
  if (!preview) {
    const empty = card("Visual evidence");
    empty.append(node("p", "", view.design ? "Preview export has not completed. Retry the preview action." : "The agent has not produced native KiCad previews yet."));
    target.append(empty);
    return;
  }
  const board = card("PCB 3D render");
  const boardImage = node("img", "preview");
  boardImage.src = sessionUrl(`/api/projects/${projectId}/artifact/board_render`);
  boardImage.alt = "Top-side KiCad 3D render of the generated board";
  board.append(boardImage); target.append(board);
  const schematic = card("Schematic preview");
  const schematicImage = node("img", "preview");
  schematicImage.src = sessionUrl(`/api/projects/${projectId}/artifact/schematic_svg`);
  schematicImage.alt = "KiCad schematic preview";
  schematic.append(schematicImage); target.append(schematic);
  const links = card("Artifacts & source");
  const linkRow = node("div", "artifact-links");
  const sourceLinks = [["schematic_pdf", "Schematic PDF"], ["board_svg", "Board SVG"], ["schematic", ".kicad_sch"], ["board", ".kicad_pcb"], ["kicad_project", ".kicad_pro"], ["requirements", "Requirements JSON"], ["ir", "Semantic IR"]];
  if (view.design?.files?.circuit_plan) sourceLinks.push(["circuit_plan", "Reviewed circuit plan"]);
  for (const [key, label] of sourceLinks) linkRow.append(artifactLink(projectId, key, label));
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
    const empty = card("Checks");
    empty.append(node("p", "", "No check results yet. The agent runs applicable real KiCad and PCBDraft checks as tool activity."));
    target.append(empty);
    return;
  }
  const readiness = card("Validation results");
  const metrics = node("div", "metric-grid");
  const candidate = node("div", "metric");
  candidate.append(node("strong", "", validation.candidate_ready ? "Checks passed" : "Findings remain"), node("span", "", "recorded checks"));
  const production = node("div", "metric");
  production.append(node("strong", "", "No compliance claim"), node("span", "", "only recorded evidence is reported"));
  metrics.append(candidate, production); readiness.append(metrics); target.append(readiness);
  const limits = card("What these checks mean");
  limits.append(node("p", "", "KiCad and PCBDraft results do not establish circuit function, electrical safety, regulatory compliance, RF/thermal behavior, sourcing, or manufacturing fitness."));
  target.append(limits);
  const noteworthy = [];
  for (const level of validation.levels || []) {
    for (const check of level.checks || []) {
      if (check.blocks_candidate && (check.outcome !== "pass" || !["completed", "not_applicable"].includes(check.state))) {
        noteworthy.push({ level: level.level, ...check });
      }
    }
  }
  if (noteworthy.length) {
    const findings = card("Findings and unavailable checks");
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

function renderProject(view) {
  state.current = view;
  if (["draft", "interpreting", "needs_clarification", "planning_required", "awaiting_confirmation", "generation_unavailable", "provider_error"].includes(view.project.status)) selectTab("brief");
  byId("empty-state").classList.add("hidden");
  byId("project-workspace").classList.remove("hidden");
  byId("project-eyebrow").textContent = `${view.project.status.replaceAll("_", " ")} · state revision ${view.state.revision} · design revision ${view.project.design_revision}`;
  byId("project-title").textContent = view.project.name;
  byId("open-kicad").disabled = !view.design;
  byId("release").disabled = !view.artifacts.validation?.candidate_ready || Boolean(activeJob(view));
  byId("message-input").disabled = Boolean(activeJob(view));
  byId("send-message").disabled = Boolean(activeJob(view));
  renderMessages(view);
  renderAgentActivity(view);
  renderBrief(view);
  renderArtifacts(view);
  renderValidation(view);
  renderProjectList();
  renderJob(view);
  window.localStorage.setItem("pcbdraft-project", view.project.id);
}

function selectTab(name) {
  for (const item of document.querySelectorAll(".tab")) {
    const selected = item.dataset.tab === name;
    item.classList.toggle("active", selected);
    item.setAttribute("aria-selected", selected ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".tab-panel")) panel.classList.toggle("hidden", panel.id !== `tab-${name}`);
}

function activeJob(view = state.current) {
  return view?.jobs?.find((job) => ["queued", "running", "cancel_requested"].includes(job.status)) || null;
}

function toolForJob(view, job) {
  const turnId = job?.args?.turn_id || job?.result?.turn_id;
  const turn = view?.agent?.turns?.find((item) => item.turn_id === turnId);
  if (!turn?.tool_runs?.length) return null;
  return [...turn.tool_runs].reverse().find((tool) => ACTIVE_TOOL_STATES.has(tool.status)) || turn.tool_runs.at(-1);
}

function renderJob(view = state.current) {
  const rememberedJob = state.activeJob?.project_id === view?.project?.id ? state.activeJob : null;
  const job = activeJob(view) || rememberedJob;
  const panel = byId("job-panel");
  if (!job || ["completed", "completed_after_cancel"].includes(job.status)) {
    state.activeJob = null;
    panel.classList.add("hidden"); return;
  }
  state.activeJob = job;
  panel.classList.remove("hidden");
  const tool = toolForJob(view, job);
  const subject = tool ? `${toolLabel(tool.tool_name)} · ${tool.tool_name}` : job.action === "agent_message" ? "Agent turn" : statusLabel(job.action);
  byId("job-title").textContent = `${subject} · ${statusLabel(job.status)}`;
  byId("job-detail").textContent = job.error || (tool ? toolDetail(tool) : "Progress is persisted; you can safely reopen this project.");
  const terminal = ["completed", "completed_after_cancel", "failed", "cancelled", "interrupted"].includes(job.status);
  byId("cancel-job").classList.toggle("hidden", terminal || job.status === "cancel_requested");
  byId("retry-job").classList.toggle("hidden", !RETRYABLE_JOB_STATES.has(job.status));
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
  const source = new EventSource(sessionUrl(`/api/projects/${projectId}/events?after=0`));
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
  byId("release").addEventListener("click", () => runAction("release"));
  byId("open-kicad").addEventListener("click", async () => {
    try { const result = await api(`/api/projects/${state.current.project.id}/open-kicad`, { method: "POST", body: {} }); toast(`Opened ${result.path}`); }
    catch (error) { toast(error.message, true); }
  });
  byId("cancel-job").addEventListener("click", cancelActiveJob);
  byId("retry-job").addEventListener("click", retryActiveJob);
  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => selectTab(tab.dataset.tab));
}

async function start() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  state.session = fragment.get("session") || "";
  bindEvents();
  try {
    await refreshProjects();
    const remembered = window.localStorage.getItem("pcbdraft-project");
    const initial = state.projects.find((item) => item.id === remembered) || state.projects[0];
    if (initial) await selectProject(initial.id);
  } catch (error) {
    byId("diagnostics").textContent = "First-run diagnostics failed.";
    toast(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", start);
