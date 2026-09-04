const DOWNLOADS = [
  "bom.csv",
  "gerbers.zip",
  "drill.zip",
  "positions.csv",
  "board.step",
  "schematic.pdf",
  "schematic.svg",
];

function clean(value, limit = 256) {
  if (typeof value !== "string") return "";
  const text = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function addFacts(target, facts) {
  const fragment = document.createDocumentFragment();
  for (const [label, value] of facts) {
    if (value === "" || value === null || value === undefined) continue;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const definition = document.createElement("dd");
    term.textContent = label;
    definition.textContent = String(value);
    row.append(term, definition);
    fragment.append(row);
  }
  target.replaceChildren(fragment);
}

function statusLabel(value, t) {
  const mapping = {
    pass: "validation.pass", warning: "validation.warning", failed: "validation.failed",
    not_run: "validation.notRun", stale: "validation.stale", unknown: "validation.unknown",
  };
  return t(mapping[value] || "validation.warning");
}

function objectKind(value, t) {
  const key = `object.kind.${value}`;
  const translated = t(key);
  return translated === key ? clean(value, 40) : translated;
}

function runtimeLabel(value, t) {
  const key = `runtime.${clean(value, 40)}`;
  const translated = t(key);
  return translated === key ? clean(value, 40) : translated;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function copyText(value, onToast, t) {
  if (!value || !navigator.clipboard?.writeText) return;
  navigator.clipboard.writeText(value).then(() => onToast(t("chat.copied"), "info")).catch(() => undefined);
}

export function createInspector({ elements, api, t, onToast }) {
  let projectId = "";
  let scene = null;
  let selection = null;
  let ipc = null;
  let validation = null;
  let artifacts = null;
  let loadVersion = 0;

  function selectTab(name) {
    for (const button of elements.tabs.querySelectorAll("[role='tab']")) {
      const active = button.dataset.inspectorTab === name;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const pane of elements.panes) pane.hidden = pane.dataset.inspectorPane !== name;
  }

  function renderOverview() {
    if (!scene) {
      addFacts(elements.overviewFacts, []);
      return;
    }
    const dimensions = `${scene.board.widthMm.toFixed(1)} × ${scene.board.heightMm.toFixed(1)} mm`;
    addFacts(elements.overviewFacts, [
      [t("overview.project"), scene.status.project || "—"],
      [t("overview.board"), dimensions],
      [t("overview.layers"), scene.board.layerCount],
      [t("overview.footprints"), scene.counts.footprints],
      [t("overview.nets"), scene.counts.nets],
      [t("overview.routes"), scene.counts.routes],
      [t("overview.vias"), scene.counts.vias],
      [t("overview.unrouted"), scene.counts.unrouted],
      [t("overview.geometryRevision"), scene.geometryRevision],
      [t("overview.designRevision"), scene.designRevision],
      [t("overview.stateRevision"), scene.stateRevision],
      [t("overview.runtime"), runtimeLabel(ipc?.status, t) || "—"],
      [t("overview.stream"), projectId ? t("overview.connected") : "—"],
    ]);
    const hash = clean(scene.contentHash, 64);
    elements.contentHash.textContent = hash ? `${hash.slice(0, 12)}…${hash.slice(-8)}` : "—";
    elements.copyHash.disabled = !hash;
    elements.copyHash.onclick = () => copyText(hash, onToast, t);
  }

  function renderObject() {
    if (!selection) {
      elements.objectTitle.textContent = t("inspector.none");
      addFacts(elements.objectFacts, []);
      return;
    }
    const common = [[t("object.type"), objectKind(selection.kind, t)]];
    if (selection.kind === "footprint") {
      elements.objectTitle.textContent = selection.reference || t("object.footprint");
      addFacts(elements.objectFacts, [...common,
        [t("object.value"), selection.value], [t("object.footprint"), selection.footprint],
        [t("object.position"), `${selection.x.toFixed(2)}, ${selection.y.toFixed(2)} mm`],
        [t("object.rotation"), `${selection.rotation.toFixed(1)}°`], [t("object.layer"), selection.side],
        [t("object.pads"), selection.pads.join(", ") || "—"],
        [t("object.nets"), selection.nets.map((item) => item.name || item.id).join(", ") || "—"],
      ]);
    } else if (selection.kind === "route") {
      const length = Math.hypot(selection.x2 - selection.x1, selection.y2 - selection.y1);
      elements.objectTitle.textContent = selection.netName || selection.net || t("object.route");
      addFacts(elements.objectFacts, [...common,
        [t("object.net"), selection.netName || selection.net], [t("object.layer"), selection.layer],
        [t("object.width"), `${selection.width.toFixed(2)} mm`], [t("object.length"), `${length.toFixed(2)} mm`],
        [t("object.endpoints"), `${selection.x1.toFixed(2)}, ${selection.y1.toFixed(2)} → ${selection.x2.toFixed(2)}, ${selection.y2.toFixed(2)} mm`],
      ]);
    } else if (selection.kind === "via") {
      elements.objectTitle.textContent = selection.netName || selection.net || t("object.via");
      addFacts(elements.objectFacts, [...common,
        [t("object.net"), selection.netName || selection.net], [t("object.position"), `${selection.x.toFixed(2)}, ${selection.y.toFixed(2)} mm`],
        [t("object.size"), `${selection.diameter.toFixed(2)} mm`], [t("object.drill"), `${selection.drill.toFixed(2)} mm`],
        [t("object.layers"), `${selection.fromLayer}–${selection.toLayer}`],
      ]);
    } else if (selection.kind === "unrouted") {
      elements.objectTitle.textContent = selection.name || selection.id || t("object.unrouted");
      addFacts(elements.objectFacts, [...common, [t("object.net"), selection.name || selection.id]]);
    } else {
      elements.objectTitle.textContent = t("object.outline");
      addFacts(elements.objectFacts, common);
    }
  }

  function renderValidation() {
    const status = validation?.state || "not_run";
    elements.validationState.textContent = statusLabel(status, t);
    elements.validationState.dataset.state = status;
    const counts = validation?.counts || {};
    addFacts(elements.validationFacts, [
      [t("validation.errors"), number(counts.error)], [t("validation.warnings"), number(counts.warning)],
      [t("validation.unconnected"), number(counts.unconnected)], [t("validation.revision"), validation?.design_revision ?? "—"],
      [t("validation.checked"), clean(validation?.checked_at, 64) || "—"],
    ]);
    const fragment = document.createDocumentFragment();
    for (const check of Array.isArray(validation?.checks) ? validation.checks : []) {
      const row = document.createElement("li");
      const key = `validation.check.${clean(check.id, 40)}`;
      const label = t(key);
      row.textContent = `${label === key ? clean(check.id, 40) : label} · ${statusLabel(clean(check.outcome, 32), t)}`;
      fragment.append(row);
    }
    elements.validationChecks.replaceChildren(fragment);
  }

  function downloadUrl(key) {
    return api.url(api.projectPath(projectId, `artifacts/${key}`)).toString();
  }

  function renderArtifacts() {
    const values = new Map((Array.isArray(artifacts?.artifacts) ? artifacts.artifacts : []).map((item) => [item.key, item]));
    const fragment = document.createDocumentFragment();
    for (const key of DOWNLOADS) {
      const item = values.get(key);
      const row = document.createElement("div");
      row.className = "artifact-row";
      const details = document.createElement("div");
      const name = document.createElement("strong");
      const note = document.createElement("span");
      name.textContent = t(`artifact.${key}`);
      const state = item?.state || "missing";
      note.textContent = state === "ready" ? `${item.file_count} · ${formatBytes(item.bytes)}` : state === "stale" ? t("fabrication.stale") : t("fabrication.missing");
      details.append(name, note);
      const action = document.createElement(item && state !== "missing" ? "a" : "button");
      action.className = "small-button";
      action.textContent = t("fabrication.download");
      if (action instanceof HTMLAnchorElement) {
        action.href = downloadUrl(key);
        action.download = "";
      } else {
        action.disabled = true;
      }
      row.append(details, action);
      fragment.append(row);
    }
    elements.artifactList.replaceChildren(fragment);
  }

  function previewUrl(name) {
    return api.url(api.projectPath(projectId, `artifacts/${name}`)).toString();
  }

  function renderPreview() {
    const available = Boolean(projectId && scene);
    elements.boardPreview.hidden = !available;
    elements.boardPreviewEmpty.hidden = available;
    if (available) {
      const boardUrl = previewUrl("board.svg");
      if (elements.boardPreview.src !== boardUrl) {
        elements.boardPreview.src = boardUrl;
      }
    }
    const schematic = new Map((Array.isArray(artifacts?.artifacts) ? artifacts.artifacts : []).map((item) => [item.key, item]));
    const schematicArtifact = schematic.get("schematic.svg");
    const hasSchematic = Boolean(
      projectId && schematicArtifact && schematicArtifact.state !== "missing",
    );
    elements.schematicPreview.hidden = !hasSchematic;
    elements.schematicPreviewEmpty.hidden = hasSchematic;
    if (hasSchematic) {
      const schematicUrl = previewUrl("schematic.svg");
      if (elements.schematicPreview.src !== schematicUrl) {
        elements.schematicPreview.src = schematicUrl;
      }
    }
  }

  async function refresh() {
    if (!projectId) return;
    const version = ++loadVersion;
    try {
      const [nextValidation, nextArtifacts] = await Promise.all([
        api.get(api.projectPath(projectId, "validation")),
        api.get(api.projectPath(projectId, "artifacts")),
      ]);
      if (version !== loadVersion) return;
      validation = nextValidation;
      artifacts = nextArtifacts;
      renderValidation();
      renderArtifacts();
      renderPreview();
    } catch (_error) {
      if (version === loadVersion) onToast(t("state.error"), "error");
    }
  }

  async function render3d() {
    if (!projectId) return;
    elements.render3d.disabled = true;
    elements.render3d.textContent = t("preview.rendering");
    try {
      await api.post(api.projectPath(projectId, "artifacts/board-3d"), {});
      elements.board3d.src = previewUrl("board-3d.png");
      elements.board3d.hidden = false;
    } catch (_error) {
      onToast(t("state.error"), "error");
    } finally {
      elements.render3d.disabled = !projectId;
      elements.render3d.textContent = t("preview.render3d");
    }
  }

  async function openKicad() {
    if (!projectId) return;
    try {
      await api.post(api.projectPath(projectId, "open-in-kicad"), {});
    } catch (_error) {
      onToast(t("state.error"), "error");
    }
  }

  for (const button of elements.tabs.querySelectorAll("[role='tab']")) {
    button.addEventListener("click", () => selectTab(button.dataset.inspectorTab));
    button.addEventListener("keydown", (event) => {
      const tabs = [...elements.tabs.querySelectorAll("[role='tab']")];
      const position = tabs.indexOf(button);
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const next = tabs[(position + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
        next.focus();
        selectTab(next.dataset.inspectorTab);
      }
    });
  }
  elements.refreshValidation.addEventListener("click", refresh);
  elements.render3d.addEventListener("click", render3d);
  elements.openKicad.addEventListener("click", openKicad);
  selectTab("overview");

  return {
    setProject(id) {
      projectId = typeof id === "string" ? id : "";
      validation = null;
      artifacts = null;
      renderValidation();
      renderArtifacts();
      renderPreview();
      return refresh();
    },
    setScene(next, nextIpc) {
      scene = next;
      ipc = nextIpc;
      renderOverview();
      renderPreview();
    },
    setSelection(next) {
      selection = next;
      renderObject();
    },
    selectTab,
    refresh,
    render3d,
    openKicad,
    refreshLabels() {
      renderOverview(); renderObject(); renderValidation(); renderArtifacts(); renderPreview();
    },
  };
}
