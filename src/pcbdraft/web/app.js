import { createApi } from "./api.js";
import { createBoardController } from "./board.js";
import { createCommandPalette, createCommandRegistry } from "./commands.js";
import { createConversation } from "./conversation.js";
import { createI18n } from "./i18n.js";
import { createInspector } from "./inspector.js";
import { createProjectRail } from "./projects.js";
import { createPreferenceStore, rememberProject } from "./store.js";

const APP_BASE = new URL(".", document.baseURI);
const API_BASE = new URL("api/", APP_BASE);
const DISCONNECTED_POLL_MIN_MS = 1000;
const DISCONNECTED_POLL_MAX_MS = 30000;
const MOBILE_LAYOUT_QUERY = "(max-width: 1179px)";
void API_BASE;

function element(selector) {
  const node = document.querySelector(selector);
  if (!node) throw new Error(`Missing workbench element: ${selector}`);
  return node;
}

const elements = {
  shell: element("#app-shell"),
  projectSelect: element("#project-select"),
  projectSearch: element("#project-search"),
  projectStatus: element("#project-status-filter"),
  projectList: element("#project-list"),
  projectRecent: element("#project-recent"),
  rail: element("#project-rail"),
  railCollapse: element("#rail-collapse"),
  railResize: element("#rail-resize"),
  inspectorPanel: element("#inspector-panel"),
  inspectorCollapse: element("#inspector-collapse"),
  inspectorResize: element("#inspector-resize"),
  commandButton: element("#command-button"),
  railToggle: element("#rail-toggle"),
  inspectorToggle: element("#inspector-toggle"),
  commandPalette: element("#command-palette"),
  commandSearch: element("#command-search"),
  commandList: element("#command-list"),
  commandClose: element("#command-close"),
  theme: element("#theme-select"),
  locale: element("#locale-select"),
  density: element("#density-select"),
  board: element("#board-svg"),
  exactBoard: element("#exact-board-layer"),
  grid: element("#grid-plane"),
  rulers: element("#ruler-layer"),
  precision: element("#preview-precision"),
  primary3d: element("#primary-3d-view"),
  view2d: element("#view-2d"),
  view3d: element("#view-3d"),
  boardEmpty: element("#board-empty"),
  boardBusy: element("#busy-banner"),
  busyMessage: element("#busy-message"),
  importExternal: element("#import-external-change"),
  boardTooltip: element("#board-tooltip"),
  fit: element("#fit-board"),
  zoomIn: element("#zoom-in"),
  zoomOut: element("#zoom-out"),
  actual: element("#actual-size"),
  centerSelection: element("#center-selection"),
  layerPreset: element("#layer-preset"),
  layerToggles: [...document.querySelectorAll("[data-layer-toggle]")],
  stream: element("#stream-indicator"),
  ipc: element("#ipc-indicator"),
  drawer: element("#agent-drawer"),
  drawerToggle: element("#drawer-toggle"),
  drawerResize: element("#drawer-resize"),
  statusProject: element("#status-project"),
  statusRevision: element("#status-revision"),
  statusBoard: element("#status-board"),
  statusNets: element("#status-nets"),
  statusRoutes: element("#status-routes"),
  statusVias: element("#status-vias"),
  statusUnrouted: element("#status-unrouted"),
  toast: element("#toast"),
};

const api = createApi({ appBase: APP_BASE });
const preferenceStore = createPreferenceStore();
const i18n = createI18n(preferenceStore.get().locale);
const state = {
  projects: [],
  selectedProject: "",
  eventSource: null,
  eventCursor: 0,
  eventStreamId: "",
  eventStreamHealthy: false,
  fallbackTimer: null,
  fallbackDelay: DISCONNECTED_POLL_MIN_MS,
  streamRecovery: null,
  snapshotInFlight: null,
  primaryView: "2d",
  primary3dBinding: "",
  externalChange: null,
  toastTimer: null,
};

function clean(value, limit = 160) {
  if (typeof value !== "string") return "";
  const result = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return result.length > limit ? `${result.slice(0, limit - 1)}…` : result;
}

function translatedProjectStatus(value) {
  const key = `projectStatus.${clean(value, 64) || "unknown"}`;
  const translated = i18n.t(key);
  return translated === key ? clean(value, 64) || i18n.t("projectStatus.unknown") : translated;
}

function showToast(message, kind = "info") {
  const translated = message === "selectionGone" ? i18n.t("board.selectionGone") : clean(message, 500);
  if (!translated) return;
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = translated;
  elements.toast.classList.toggle("error", kind === "error");
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 4200);
}

function updateIndicator(node, label, level) {
  node.classList.remove("status-good", "status-warn", "status-bad");
  node.classList.add(`status-${level}`);
  const textNode = node.querySelector("span");
  const key = `runtime.${clean(label, 42)}`;
  const translated = i18n.t(key);
  if (textNode) textNode.textContent = translated === key ? clean(label, 42) || "—" : translated;
}

function projectById(id) {
  return state.projects.find((project) => project.id === id) || null;
}

function renderProjectSelect() {
  const fragment = document.createDocumentFragment();
  const prompt = document.createElement("option");
  prompt.value = "";
  prompt.textContent = state.projects.length ? i18n.t("rail.open") : i18n.t("rail.empty");
  fragment.append(prompt);
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} · ${translatedProjectStatus(project.status)}`;
    fragment.append(option);
  }
  elements.projectSelect.replaceChildren(fragment);
  elements.projectSelect.value = projectById(state.selectedProject) ? state.selectedProject : "";
}

function applyTheme() {
  const choice = preferenceStore.get().theme;
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = choice === "system" ? (dark ? "dark" : "light") : choice;
  document.documentElement.dataset.themeChoice = choice;
  elements.theme.value = choice;
}

function applyPreferenceLayout() {
  const value = preferenceStore.get();
  elements.shell.style.setProperty("--rail-width", `${value.railWidth}px`);
  elements.shell.style.setProperty("--inspector-width", `${value.inspectorWidth}px`);
  elements.shell.style.setProperty("--drawer-height", `${value.drawerHeight}px`);
  elements.shell.classList.toggle("rail-collapsed", value.panels.railCollapsed);
  elements.shell.classList.toggle("inspector-collapsed", value.panels.inspectorCollapsed);
  elements.drawer.classList.toggle("is-collapsed", !value.panels.drawerOpen);
  elements.railCollapse.setAttribute("aria-expanded", String(!value.panels.railCollapsed));
  elements.inspectorCollapse.setAttribute("aria-expanded", String(!value.panels.inspectorCollapsed));
  elements.drawerToggle.setAttribute("aria-expanded", String(value.panels.drawerOpen));
  elements.railResize.setAttribute("aria-valuenow", String(value.railWidth));
  elements.inspectorResize.setAttribute("aria-valuenow", String(value.inspectorWidth));
  elements.drawerResize.setAttribute("aria-valuenow", String(value.drawerHeight));
  elements.drawerToggle.textContent = i18n.t(value.panels.drawerOpen ? "drawer.close" : "drawer.open");
  document.documentElement.dataset.density = value.density;
  elements.shell.dataset.density = value.density;
  elements.density.value = value.density;
  for (const button of elements.layerToggles) {
    const visible = value.layers[button.dataset.layerToggle] !== false;
    button.setAttribute("aria-pressed", String(visible));
    button.classList.toggle("is-active", visible);
  }
  applyTheme();
}

function patchPreferences(patch) {
  return preferenceStore.update(patch);
}

function patchPanel(name, value) {
  const current = preferenceStore.get();
  patchPreferences({ panels: { ...current.panels, [name]: value } });
}

function isMobileLayout() {
  return window.matchMedia(MOBILE_LAYOUT_QUERY).matches;
}

function currentMobileOverlay() {
  if (elements.shell.classList.contains("mobile-rail-open")) return "rail";
  if (elements.shell.classList.contains("mobile-inspector-open")) return "inspector";
  return null;
}

function setMobileOverlay(overlay) {
  elements.shell.classList.toggle("mobile-rail-open", overlay === "rail");
  elements.shell.classList.toggle("mobile-inspector-open", overlay === "inspector");
  elements.railToggle.setAttribute("aria-expanded", String(overlay === "rail"));
  elements.inspectorToggle.setAttribute("aria-expanded", String(overlay === "inspector"));
}

function persistentPanelName(panel) {
  return panel === "rail" ? "railCollapsed" : "inspectorCollapsed";
}

function togglePanel(panel) {
  if (isMobileLayout()) {
    const current = currentMobileOverlay();
    setMobileOverlay(current === panel ? null : panel);
    return;
  }
  const name = persistentPanelName(panel);
  patchPanel(name, !preferenceStore.get().panels[name]);
}

function collapsePanel(panel) {
  if (isMobileLayout()) {
    if (currentMobileOverlay() === panel) setMobileOverlay(null);
    return;
  }
  const name = persistentPanelName(panel);
  patchPanel(name, !preferenceStore.get().panels[name]);
}

function openProjectRail() {
  if (isMobileLayout()) {
    setMobileOverlay("rail");
  } else {
    patchPanel("railCollapsed", false);
  }
  elements.projectSearch.focus();
}

function openInspectorTab(tab) {
  if (isMobileLayout()) {
    setMobileOverlay("inspector");
  } else {
    patchPanel("inspectorCollapsed", false);
  }
  inspector.selectTab(tab);
}

function boundArtifactUrl(projectId, name, scene) {
  const url = api.url(api.projectPath(projectId, `artifacts/${name}`));
  url.searchParams.set("revision", String(scene.designRevision));
  url.searchParams.set("content_hash", scene.contentHash);
  return url.toString();
}

let inspector;
const board = createBoardController({
  svg: elements.board,
  viewport: element("#board-stage"),
  tooltip: elements.boardTooltip,
  exactLayer: elements.exactBoard,
  precisionLabel: elements.precision,
  gridGroup: elements.grid,
  rulerGroup: elements.rulers,
  layerGroups: {
    board: element("#board-fill-layer"), outline: element("#outline-layer"), front: element("#front-layer"),
    back: element("#back-layer"), via: element("#via-layer"), footprint: element("#footprint-layer"),
    pad: element("#pad-layer"), label: element("#label-layer"), unrouted: element("#unrouted-layer"),
    finding: element("#finding-layer"),
  },
  exactUrl: (scene) => boundArtifactUrl(scene.projectId, "board.svg", scene),
  onSelection(next) {
    inspector?.setSelection(next);
  },
  onStatus(next) {
    renderStatus(next);
  },
  onToast: showToast,
  preferences: () => preferenceStore.get(),
  savePreferences(next) {
    preferenceStore.replace(next);
  },
  t: i18n.t,
});

inspector = createInspector({
  elements: {
    tabs: element("#inspector-tabs"),
    panes: [...document.querySelectorAll("[data-inspector-pane]")],
    overviewFacts: element("#overview-facts"), contentHash: element("#content-hash"), copyHash: element("#copy-hash"),
    objectTitle: element("#object-title"), objectFacts: element("#object-facts"), validationState: element("#validation-state"),
    refreshValidation: element("#refresh-validation"), validationFacts: element("#validation-facts"), validationChecks: element("#validation-checks"),
    artifactList: element("#artifact-list"), boardPreview: element("#exact-board-preview"), boardPreviewEmpty: element("#board-preview-empty"),
    schematicPreview: element("#schematic-preview"), schematicPreviewEmpty: element("#schematic-preview-empty"),
    board3d: element("#exact-3d-preview"), render3d: element("#load-3d-preview"), openKicad: element("#open-in-kicad"),
  },
  api, t: i18n.t, onToast: showToast,
  onLocate(finding) {
    if (!board.locateFinding(finding)) showToast(i18n.t("validation.locationUnavailable"), "warning");
  },
});

const conversation = createConversation({
  elements: {
    tabs: element("#drawer-tabs"), conversationPane: element("#conversation-pane"), activityPane: element("#activity-pane"),
    chatLog: element("#chat-log"), chatEmpty: element("#chat-empty"), turnState: element("#turn-state"),
    form: element("#message-form"), input: element("#message-input"), send: element("#send-message"), stop: element("#stop-turn"),
    activityList: element("#activity-list"), activityEmpty: element("#activity-empty"), activityType: element("#activity-type-filter"), activityState: element("#activity-state-filter"),
  },
  api, t: i18n.t, onToast: showToast,
});

const rail = createProjectRail({
  search: elements.projectSearch,
  status: elements.projectStatus,
  list: elements.projectList,
  recent: elements.projectRecent,
  onOpen: openProject,
  t: i18n.t,
});

function renderStatus(scene) {
  if (!scene) return;
  elements.statusProject.textContent = scene.projectId || "—";
  elements.statusRevision.textContent = `${i18n.t("status.revision")}: ${scene.designRevision}`;
  elements.statusBoard.textContent = `${i18n.t("status.board")}: ${scene.board.widthMm.toFixed(1)}×${scene.board.heightMm.toFixed(1)} mm`;
  elements.statusNets.textContent = `${i18n.t("status.nets")}: ${scene.counts.nets}`;
  elements.statusRoutes.textContent = `${i18n.t("status.routes")}: ${scene.counts.routes}`;
  elements.statusVias.textContent = `${i18n.t("status.vias")}: ${scene.counts.vias}`;
  elements.statusUnrouted.textContent = `${i18n.t("status.unrouted")}: ${scene.counts.unrouted}`;
}

function showPrimary2d() {
  state.primaryView = "2d";
  elements.board.hidden = false;
  elements.primary3d.hidden = true;
  elements.view2d.setAttribute("aria-pressed", "true");
  elements.view3d.setAttribute("aria-pressed", "false");
  board.refreshPrecision();
}

async function showPrimary3d() {
  const projectId = state.selectedProject;
  const scene = board.getScene();
  if (!projectId || !scene?.contentHash) return;
  elements.view3d.disabled = true;
  elements.precision.textContent = i18n.t("preview.rendering");
  try {
    const result = await api.post(api.projectPath(projectId, "artifacts/board-3d"), {});
    const current = board.getScene();
    if (
      projectId !== state.selectedProject
      || !current
      || result?.design_revision !== current.designRevision
      || result?.content_hash !== current.contentHash
    ) throw new Error("3D preview binding changed during render");
    const binding = `${current.designRevision}:${current.contentHash}`;
    elements.primary3d.src = boundArtifactUrl(projectId, "board-3d.png", current);
    elements.primary3d.hidden = false;
    elements.board.hidden = true;
    state.primaryView = "3d";
    state.primary3dBinding = binding;
    elements.view2d.setAttribute("aria-pressed", "false");
    elements.view3d.setAttribute("aria-pressed", "true");
    elements.precision.textContent = i18n.t("board.exact3d");
  } catch (error) {
    showPrimary2d();
    showToast(clean(error?.message, 500) || i18n.t("preview.unavailable"), "warning");
  } finally {
    elements.view3d.disabled = false;
  }
}

elements.primary3d.addEventListener("error", () => {
  if (state.primaryView !== "3d") return;
  showPrimary2d();
  showToast(i18n.t("preview.unavailable"), "warning");
});

function stopEvents() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
  state.eventStreamHealthy = false;
  stopDisconnectedPolling();
}

function startEvents() {
  stopEvents();
  if (!state.selectedProject) return;
  const url = api.url(api.projectPath(state.selectedProject, "events"));
  url.searchParams.set("after", String(state.eventCursor));
  const source = new EventSource(url);
  state.eventSource = source;
  updateIndicator(elements.stream, "connecting", "warn");
  source.addEventListener("open", () => {
    if (state.eventSource !== source) return;
    state.eventStreamHealthy = true;
    state.fallbackDelay = DISCONNECTED_POLL_MIN_MS;
    stopDisconnectedPolling();
    updateIndicator(elements.stream, "live", "good");
  });
  source.addEventListener("update", (event) => {
    try {
      const value = JSON.parse(event.data);
      const sequence = Number(value.sequence);
      const streamId = clean(value.stream_id, 64);
      const hasGap = !Number.isInteger(sequence) || sequence !== state.eventCursor + 1;
      const changedStream = Boolean(state.eventStreamId && streamId !== state.eventStreamId);
      if (value.kind === "stream.reset_required" || hasGap || changedStream) {
        recoverEventStream();
        return;
      }
      state.eventCursor = sequence;
      state.eventStreamId = streamId;
      conversation.addEvent(value);
      if (["scene.committed", "generation.complete", "validation.complete", "job.complete", "job.failed"].includes(value.kind)) {
        refreshSnapshot({ quiet: true });
      }
    } catch (_error) {
      state.eventStreamHealthy = false;
      updateIndicator(elements.stream, "reconnecting", "warn");
      scheduleDisconnectedPolling();
    }
  });
  source.addEventListener("error", () => {
    if (state.eventSource !== source) return;
    state.eventStreamHealthy = false;
    updateIndicator(elements.stream, "reconnecting", "warn");
    scheduleDisconnectedPolling();
  });
}

function stopDisconnectedPolling() {
  if (state.fallbackTimer) window.clearTimeout(state.fallbackTimer);
  state.fallbackTimer = null;
}

function scheduleDisconnectedPolling() {
  if (state.eventStreamHealthy || !state.selectedProject || state.fallbackTimer) return;
  const delay = state.fallbackDelay;
  state.fallbackTimer = window.setTimeout(async () => {
    state.fallbackTimer = null;
    if (state.eventStreamHealthy || !state.selectedProject) return;
    await refreshSnapshot({ quiet: true });
    state.fallbackDelay = Math.min(DISCONNECTED_POLL_MAX_MS, delay * 2);
    scheduleDisconnectedPolling();
  }, delay);
}

function recoverEventStream() {
  if (state.streamRecovery) return state.streamRecovery;
  const projectId = state.selectedProject;
  stopEvents();
  updateIndicator(elements.stream, "reconnecting", "warn");
  state.streamRecovery = refreshSnapshot({ quiet: true, resetStreamCursor: true })
    .finally(() => {
      state.streamRecovery = null;
      if (projectId === state.selectedProject) startEvents();
    });
  return state.streamRecovery;
}

async function refreshSnapshot({ quiet = false, resetStreamCursor = false } = {}) {
  if (!state.selectedProject) return null;
  const running = state.snapshotInFlight;
  if (running) return running.promise;
  const projectId = state.selectedProject;
  const promise = api.get(api.projectPath(projectId, "snapshot")).then((payload) => {
    if (projectId !== state.selectedProject) return null;
    const busy = payload?.busy === true;
    const externalChange = payload?.external_change;
    state.externalChange = externalChange && typeof externalChange === "object" ? externalChange : null;
    const previous = board.getScene();
    const updated = payload?.scene ? board.setScene(payload.scene) : false;
    const scene = board.getScene();
    elements.boardBusy.hidden = !busy;
    const externalPending = state.externalChange?.requires_import === true;
    elements.busyMessage.textContent = externalPending
      ? clean(state.externalChange?.limitation, 500) || i18n.t("board.externalDetected")
      : i18n.t("board.busy");
    elements.importExternal.hidden = !(
      externalPending && state.externalChange?.importable === true
    );
    elements.boardEmpty.hidden = Boolean(scene);
    if (updated && scene) inspector.setScene(scene, payload?.ipc);
    if (updated && scene && previous && (previous.designRevision !== scene.designRevision || previous.contentHash !== scene.contentHash)) showPrimary2d();
    if (payload?.session) conversation.setSession(payload.session);
    if (resetStreamCursor && Number.isInteger(payload?.stream?.last_sequence)) {
      state.eventCursor = payload.stream.last_sequence;
      state.eventStreamId = clean(payload.stream.stream_id, 64);
    }
    const ipcState = clean(payload?.ipc?.status?.state || payload?.ipc?.status, 42) || "disabled";
    updateIndicator(elements.ipc, ipcState, ipcState === "online" ? "good" : "warn");
    return payload;
  }).catch((_error) => {
    if (!quiet) showToast(i18n.t("state.error"), "error");
    return null;
  }).finally(() => {
    if (state.snapshotInFlight?.projectId === projectId) state.snapshotInFlight = null;
  });
  state.snapshotInFlight = { projectId, promise };
  return promise;
}

async function importExternalChange() {
  const change = state.externalChange;
  if (!state.selectedProject || change?.importable !== true || !Number.isInteger(change.canonical_revision)) return;
  elements.importExternal.disabled = true;
  try {
    await api.post(
      api.projectPath(state.selectedProject, "external-change/import"),
      { expected_revision: change.canonical_revision },
    );
    state.externalChange = null;
    await recoverEventStream();
  } catch (error) {
    showToast(clean(error?.message, 500) || i18n.t("state.error"), "error");
  } finally {
    elements.importExternal.disabled = false;
  }
}

async function loadProjects(bootstrap = {}) {
  const payload = bootstrap.projects ? bootstrap : await api.get("projects");
  state.projects = rail.setProjects(payload);
  renderProjectSelect();
  const initial = clean(bootstrap.initial_project, 80);
  const remembered = preferenceStore.get().recentProjects;
  const preferred = [initial, ...remembered, state.projects[0]?.id].find((id) => projectById(id));
  rail.setRemembered(remembered);
  if (preferred) await openProject(preferred);
}

async function openProject(projectId) {
  if (!projectById(projectId)) return;
  if (isMobileLayout() && currentMobileOverlay() === "rail") setMobileOverlay(null);
  stopEvents();
  state.selectedProject = projectId;
  state.eventCursor = 0;
  state.eventStreamId = "";
  state.fallbackDelay = DISCONNECTED_POLL_MIN_MS;
  const current = preferenceStore.get();
  preferenceStore.replace(rememberProject(current, projectId));
  rail.setSelected(projectId);
  rail.setRemembered(preferenceStore.get().recentProjects);
  renderProjectSelect();
  board.clear();
  showPrimary2d();
  elements.boardEmpty.hidden = false;
  updateIndicator(elements.stream, "connecting", "warn");
  // Establish one complete canonical frame before secondary read models begin
  // loading.  Those reads share the project lock and must not make the initial
  // snapshot look like an authoritative write is still in progress.
  await refreshSnapshot({ resetStreamCursor: true });
  await Promise.all([
    inspector.setProject(projectId),
    conversation.setProject(projectId),
  ]);
  startEvents();
}

function cycle(field, options) {
  const current = preferenceStore.get()[field];
  const next = options[(options.indexOf(current) + 1) % options.length];
  patchPreferences({ [field]: next });
}

const LAYER_PRESETS = ["all", "front", "back", "assembly", "routing"];

function setLayerPreset(name) {
  if (!LAYER_PRESETS.includes(name)) return;
  elements.layerPreset.value = name;
  board.setPreset(name);
}

function cycleLayerPreset() {
  const current = elements.layerPreset.value;
  const next = LAYER_PRESETS[(LAYER_PRESETS.indexOf(current) + 1) % LAYER_PRESETS.length];
  setLayerPreset(next);
}

const registry = createCommandRegistry([
  { id: "switch-project", label: "command.switchProject", shortcut: "Ctrl K", run: () => openProjectRail() },
  { id: "fit", label: "command.fit", shortcut: "F", run: () => board.fit(), available: () => Boolean(board.getScene()) },
  { id: "validation", label: "command.openValidation", run: () => openInspectorTab("validation") },
  { id: "fabrication", label: "command.openFabrication", run: () => openInspectorTab("fabrication") },
  { id: "drawer", label: "command.toggleDrawer", run: () => patchPanel("drawerOpen", !preferenceStore.get().panels.drawerOpen) },
  { id: "layer-preset", label: "command.cycleLayerPreset", run: () => cycleLayerPreset() },
  { id: "theme", label: "command.theme", run: () => cycle("theme", ["system", "dark", "light"]) },
  { id: "locale", label: "command.locale", run: () => cycle("locale", ["zh-CN", "en"]) },
  { id: "density", label: "command.density", run: () => cycle("density", ["comfortable", "compact"]) },
  { id: "refresh", label: "command.refresh", shortcut: "R", run: () => refreshSnapshot() },
  { id: "render-3d", label: "command.render3d", run: () => inspector.render3d(), available: () => Boolean(state.selectedProject) },
  { id: "open-kicad", label: "command.openKicad", run: () => inspector.openKicad(), available: () => Boolean(state.selectedProject) },
]);

const palette = createCommandPalette({
  dialog: elements.commandPalette, search: elements.commandSearch, list: elements.commandList, close: elements.commandClose,
  registry, context: () => ({ projectId: state.selectedProject, scene: board.getScene() }), t: i18n.t,
});

function refreshLabels() {
  i18n.apply();
  elements.locale.value = i18n.locale;
  rail.refreshLabels();
  inspector.refreshLabels();
  conversation.refreshLabels();
  renderProjectSelect();
  renderStatus(board.getScene());
  palette.render();
  applyPreferenceLayout();
}

preferenceStore.subscribe((preferences) => {
  i18n.setLocale(preferences.locale);
  applyPreferenceLayout();
  board.refreshLayerVisibility();
  refreshLabels();
});

elements.projectSelect.addEventListener("change", () => openProject(elements.projectSelect.value));
elements.commandButton.addEventListener("click", () => palette.show());
elements.railToggle.addEventListener("click", () => togglePanel("rail"));
elements.inspectorToggle.addEventListener("click", () => togglePanel("inspector"));
elements.theme.addEventListener("change", () => patchPreferences({ theme: elements.theme.value }));
elements.locale.addEventListener("change", () => patchPreferences({ locale: elements.locale.value }));
elements.density.addEventListener("change", () => patchPreferences({ density: elements.density.value }));
elements.railCollapse.addEventListener("click", () => collapsePanel("rail"));
elements.inspectorCollapse.addEventListener("click", () => collapsePanel("inspector"));
elements.drawerToggle.addEventListener("click", () => patchPanel("drawerOpen", !preferenceStore.get().panels.drawerOpen));
elements.fit.addEventListener("click", () => board.fit());
elements.zoomIn.addEventListener("click", () => board.zoomIn());
elements.zoomOut.addEventListener("click", () => board.zoomOut());
elements.actual.addEventListener("click", () => board.actualSize());
elements.centerSelection.addEventListener("click", () => board.centerSelection());
elements.importExternal.addEventListener("click", importExternalChange);
elements.view2d.addEventListener("click", showPrimary2d);
elements.view3d.addEventListener("click", showPrimary3d);
elements.layerPreset.addEventListener("change", () => setLayerPreset(elements.layerPreset.value));
for (const button of elements.layerToggles) {
  button.addEventListener("click", () => board.toggleLayer(button.dataset.layerToggle));
}

const PANEL_RESIZE = {
  railWidth: { css: "--rail-width", minimum: 220, maximum: 320, direction: 1 },
  inspectorWidth: { css: "--inspector-width", minimum: 300, maximum: 460, direction: -1 },
};

function clampResize(value, { minimum, maximum }) {
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function setPanelWidth(field, value) {
  const config = PANEL_RESIZE[field];
  if (!config) return 0;
  const width = clampResize(value, config);
  elements.shell.style.setProperty(config.css, `${width}px`);
  return width;
}

function resizePanelWithKeys(event, field) {
  const config = PANEL_RESIZE[field];
  if (!config) return;
  const current = preferenceStore.get()[field];
  let next = current;
  if (event.key === "Home") next = config.minimum;
  else if (event.key === "End") next = config.maximum;
  else if (event.key === "ArrowLeft") next += -8 * config.direction;
  else if (event.key === "ArrowRight") next += 8 * config.direction;
  else return;
  event.preventDefault();
  patchPreferences({ [field]: setPanelWidth(field, next) });
}

let drawerResize = null;
let panelResize = null;

function startPanelResize(event, field) {
  if (event.button !== 0 || isMobileLayout()) return;
  const config = PANEL_RESIZE[field];
  if (!config) return;
  event.preventDefault();
  panelResize = {
    field,
    startX: event.clientX,
    width: preferenceStore.get()[field],
    target: event.currentTarget,
  };
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

for (const [target, field] of [[elements.railResize, "railWidth"], [elements.inspectorResize, "inspectorWidth"]]) {
  target.addEventListener("pointerdown", (event) => startPanelResize(event, field));
  target.addEventListener("keydown", (event) => resizePanelWithKeys(event, field));
}

elements.drawerResize.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  drawerResize = { y: event.clientY, height: preferenceStore.get().drawerHeight };
  elements.drawerResize.setPointerCapture?.(event.pointerId);
});
elements.drawerResize.addEventListener("keydown", (event) => {
  const current = preferenceStore.get().drawerHeight;
  let next = current;
  if (event.key === "Home") next = 180;
  else if (event.key === "End") next = 720;
  else if (event.key === "ArrowUp") next += 12;
  else if (event.key === "ArrowDown") next -= 12;
  else return;
  event.preventDefault();
  const height = Math.min(720, Math.max(180, next));
  elements.shell.style.setProperty("--drawer-height", `${height}px`);
  patchPreferences({ drawerHeight: height });
});
window.addEventListener("pointermove", (event) => {
  if (panelResize) {
    const config = PANEL_RESIZE[panelResize.field];
    const width = panelResize.width + (event.clientX - panelResize.startX) * config.direction;
    setPanelWidth(panelResize.field, width);
    return;
  }
  if (!drawerResize) return;
  const height = Math.min(720, Math.max(180, drawerResize.height + (drawerResize.y - event.clientY)));
  elements.shell.style.setProperty("--drawer-height", `${Math.round(height)}px`);
});
window.addEventListener("pointerup", (event) => {
  if (panelResize) {
    const { field, target } = panelResize;
    const config = PANEL_RESIZE[field];
    const width = Number.parseInt(elements.shell.style.getPropertyValue(config.css), 10);
    if (Number.isFinite(width)) patchPreferences({ [field]: setPanelWidth(field, width) });
    panelResize = null;
    if (target.hasPointerCapture?.(event.pointerId)) target.releasePointerCapture(event.pointerId);
    return;
  }
  if (!drawerResize) return;
  const height = Number.parseInt(elements.shell.style.getPropertyValue("--drawer-height"), 10);
  if (Number.isFinite(height)) patchPreferences({ drawerHeight: height });
  drawerResize = null;
  if (elements.drawerResize.hasPointerCapture?.(event.pointerId)) elements.drawerResize.releasePointerCapture(event.pointerId);
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    palette.toggle();
    return;
  }
  if (event.defaultPrevented || palette.open) return;
  if (event.key === "Escape" && isMobileLayout() && currentMobileOverlay()) {
    event.preventDefault();
    setMobileOverlay(null);
    return;
  }
  const editable = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement;
  if (editable) return;
  if (event.key === "Escape") board.clearSelection();
  if (event.key.toLowerCase() === "f") { event.preventDefault(); board.fit(); }
  if (event.key === "0") board.actualSize();
  if (event.key === "1") setLayerPreset("front");
  if (event.key === "2") setLayerPreset("back");
});

window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
  if (preferenceStore.get().theme === "system") applyTheme();
});
window.matchMedia(MOBILE_LAYOUT_QUERY).addEventListener("change", (event) => {
  if (!event.matches) setMobileOverlay(null);
});
window.addEventListener("beforeunload", () => { stopEvents(); });

async function bootstrap() {
  i18n.setLocale(preferenceStore.get().locale);
  applyPreferenceLayout();
  refreshLabels();
  try {
    const payload = await api.get("bootstrap");
    api.setCsrfToken(payload.csrf_token);
    await loadProjects(payload);
  } catch (_error) {
    showToast(i18n.t("state.error"), "error");
  }
}

bootstrap();
