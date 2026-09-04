const SVG_NS = "http://www.w3.org/2000/svg";
export const MAX_SCENE_ITEMS = 5000;

function text(value, limit = 160) {
  if (typeof value !== "string") return "";
  const clean = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1)}…` : clean;
}

function number(value, fallback = 0) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function nonNegative(value, fallback = 0) {
  const parsed = number(value, fallback);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function rows(value, limit) {
  return Array.isArray(value) ? value.slice(0, limit).filter((row) => row && typeof row === "object") : [];
}

function objectId(prefix, value, index) {
  const candidate = text(value, 128);
  return `${prefix}:${candidate || index}`;
}

function normalizeNets(value) {
  return rows(value, 128).flatMap((item) => {
    const id = text(item.id, 128);
    const name = text(item.name, 128);
    return id || name ? [{ id, name }] : [];
  });
}

export function normalizeScene(payload) {
  const source = payload?.scene || payload?.snapshot?.scene || payload?.snapshot || payload;
  if (!source || typeof source !== "object") return null;
  if (source.schema && source.schema !== "pcbdraft-live-board-scene") return null;
  const boardSource = source.board && typeof source.board === "object" ? source.board : {};
  const board = {
    widthMm: Math.max(1, number(boardSource.width_mm, 80)),
    heightMm: Math.max(1, number(boardSource.height_mm, 50)),
    layerCount: Math.max(1, nonNegative(boardSource.layer_count, Array.isArray(boardSource.layers) ? boardSource.layers.length : 2)),
    layers: rows(boardSource.layers, 32).map((layer) => text(layer, 64)).filter(Boolean),
  };
  let remainingItems = MAX_SCENE_ITEMS;
  function sceneRows(value, hardLimit = remainingItems) {
    const result = rows(value, Math.min(hardLimit, remainingItems));
    remainingItems -= result.length;
    return result;
  }
  const outlineSource = Array.isArray(source.outline) ? source.outline : boardSource.outline;
  const outlines = sceneRows(outlineSource, 128).map((item, index) => ({
    key: objectId("outline", item.id || item.uuid, index),
    kind: "outline",
    id: text(item.id, 128), uuid: text(item.uuid, 128),
    x1: number(item.x1_mm), y1: number(item.y1_mm), x2: number(item.x2_mm), y2: number(item.y2_mm),
  }));
  const footprints = sceneRows(source.footprints).map((item, index) => ({
    key: objectId("footprint", item.id || item.reference, index), kind: "footprint",
    id: text(item.id, 128), uuid: text(item.uuid, 128), reference: text(item.reference || item.label, 64), value: text(item.value, 160),
    footprint: text(item.footprint, 256), x: number(item.x_mm), y: number(item.y_mm),
    rotation: number(item.rotation_deg), side: text(item.side, 24), placed: item.placed === true,
    pads: (Array.isArray(item.pads) ? item.pads : []).slice(0, 128).map((pad) => text(pad, 64)).filter(Boolean),
    padGeometry: rows(item.pad_geometry, 256).map((pad, padIndex) => ({
      key: objectId("pad", pad.id || pad.uuid, padIndex), kind: "pad", id: text(pad.id, 160), uuid: text(pad.uuid, 128),
      number: text(pad.number, 64), componentId: text(pad.component_id, 128), net: text(pad.net, 128), netName: text(pad.net_name, 128),
      x: number(pad.x_mm), y: number(pad.y_mm), width: Math.max(0.01, number(pad.width_mm, 0.1)), height: Math.max(0.01, number(pad.height_mm, 0.1)),
      layers: (Array.isArray(pad.layers) ? pad.layers : []).slice(0, 32).map((layer) => text(layer, 64)).filter(Boolean),
    })),
    bounds: item.bounds && typeof item.bounds === "object" ? {
      x: number(item.bounds.x_mm), y: number(item.bounds.y_mm), width: Math.max(0.01, number(item.bounds.width_mm, 4)), height: Math.max(0.01, number(item.bounds.height_mm, 3)),
    } : null,
    boundsSource: text(item.bounds_source, 64), nets: normalizeNets(item.nets),
  }));
  const pads = footprints.flatMap((item) => item.padGeometry).slice(0, remainingItems);
  remainingItems -= pads.length;
  const routes = sceneRows(source.routes).map((item, index) => ({
    key: objectId("route", item.id, index), kind: "route", id: text(item.id, 128), uuid: text(item.uuid, 128), net: text(item.net, 128), netName: text(item.net_name, 128),
    layer: text(item.layer, 64), width: Math.max(0.05, number(item.width_mm, 0.2)),
    x1: number(item.x1_mm), y1: number(item.y1_mm), x2: number(item.x2_mm), y2: number(item.y2_mm),
  }));
  const vias = sceneRows(source.vias).map((item, index) => ({
    key: objectId("via", item.id, index), kind: "via", id: text(item.id, 128), uuid: text(item.uuid, 128), net: text(item.net, 128), netName: text(item.net_name, 128),
    x: number(item.x_mm), y: number(item.y_mm), diameter: Math.max(0.1, number(item.diameter_mm, 0.6)), drill: Math.max(0, number(item.drill_mm, 0)),
    fromLayer: nonNegative(item.from_layer), toLayer: nonNegative(item.to_layer),
  }));
  const unrouted = sceneRows(source.unrouted_nets).map((item, index) => ({
    key: objectId("unrouted", item.id || item.name, index), kind: "unrouted", id: text(item.id, 128), name: text(item.name, 128),
    endpoints: rows(item.endpoints, 64).map((endpoint) => ({ reference: text(endpoint.reference, 64), x: number(endpoint.x_mm), y: number(endpoint.y_mm) })),
  }));
  const status = source.status && typeof source.status === "object" ? source.status : {};
  const counts = status.counts && typeof status.counts === "object" ? status.counts : {};
  const primary = source.canvas?.primary && typeof source.canvas.primary === "object" ? source.canvas.primary : {};
  return {
    projectId: text(source.project_id, 80),
    contentHash: text(source.content_hash, 64),
    stateRevision: nonNegative(source.state_revision),
    designRevision: nonNegative(source.design_revision),
    geometryRevision: nonNegative(source.geometry_revision),
    board, outlines, footprints, pads, routes, vias, unrouted,
    canvas: {
      exact: primary.kind === "kicad_svg" && primary.state === "available_on_request" && text(primary.content_hash, 64) === text(source.content_hash, 64),
      canonicalRevision: nonNegative(primary.canonical_revision),
      designRevision: nonNegative(primary.design_revision),
      contentHash: text(primary.content_hash, 64),
      fallbackLabel: text(source.canvas?.fallback?.label, 96) || "近似预览 / approximate preview",
    },
    status: { project: text(status.project_status, 80), validation: status.validation || null },
    counts: {
      footprints: nonNegative(counts.footprints, footprints.length), routes: nonNegative(counts.routes, routes.length),
      vias: nonNegative(counts.vias, vias.length), unrouted: nonNegative(counts.unrouted_nets, unrouted.length),
      nets: nonNegative(counts.nets, new Set([...routes, ...vias].map((item) => item.net).filter(Boolean)).size),
    },
  };
}

function svg(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function title(node, value) {
  let child = node.querySelector("title");
  if (!child) {
    child = svg("title");
    node.prepend(child);
  }
  child.textContent = text(value, 240);
}

function reconcile(group, objects, make, update) {
  const existing = new Map([...group.children].map((node) => [node.dataset.objectKey, node]));
  const fragment = document.createDocumentFragment();
  for (const object of objects) {
    let node = existing.get(object.key);
    if (!node) node = make(object);
    update(node, object);
    fragment.append(node);
    existing.delete(object.key);
  }
  for (const stale of existing.values()) stale.remove();
  group.replaceChildren(fragment);
}

function footprintTitle(item) {
  return [item.reference, item.value, item.footprint].filter(Boolean).join(" · ");
}

function routeTitle(item) {
  return [item.netName || item.net, item.layer, `${item.width.toFixed(2)} mm`].filter(Boolean).join(" · ");
}

function viaTitle(item) {
  return [item.netName || item.net, `${item.diameter.toFixed(2)} / ${item.drill.toFixed(2)} mm`].filter(Boolean).join(" · ");
}

function padTitle(item) {
  return [item.componentId, item.number, item.netName || item.net].filter(Boolean).join(" · ");
}

function objectSummary(item, t) {
  if (item.kind === "footprint") return footprintTitle(item);
  if (item.kind === "route") return routeTitle(item);
  if (item.kind === "via") return viaTitle(item);
  if (item.kind === "pad") return padTitle(item);
  if (item.kind === "finding") return item.message || item.type;
  if (item.kind === "unrouted") return item.name || item.id;
  return t("board.outline");
}

export function createBoardController({ svg: boardSvg, viewport, tooltip, exactLayer, precisionLabel, gridGroup, rulerGroup, layerGroups, exactUrl, onSelection, onStatus, onToast, preferences, savePreferences, t }) {
  let scene = null;
  let selection = null;
  let dragging = null;
  let exactToken = "";
  let exactReady = false;
  let camera = { x: -5, y: -5, width: 90, height: 60 };
  const objectMap = new Map();
  const identityMap = new Map();
  const nodes = new Map();
  const groups = layerGroups;

  function applyCamera() {
    boardSvg.setAttribute("viewBox", `${camera.x} ${camera.y} ${camera.width} ${camera.height}`);
  }

  function updateVisibility() {
    const layerState = preferences().layers;
    for (const [name, group] of Object.entries(groups)) {
      const key = name === "via" ? "vias" : ["footprint", "pad"].includes(name) ? "footprints" : name === "label" ? "labels" : name;
      group.classList.toggle("hidden", layerState[key] === false);
    }
  }

  function setPrecision(mode) {
    const exact = mode === "exact";
    precisionLabel.textContent = exact ? t("board.exact") : scene?.canvas?.fallbackLabel || t("board.approximate");
    precisionLabel.classList.toggle("exact", exact);
    precisionLabel.classList.toggle("approximate", !exact);
  }

  function updateExactCanvas() {
    const usable = Boolean(scene?.canvas?.exact && scene.contentHash && typeof exactUrl === "function");
    const token = usable ? `${scene.designRevision}:${scene.contentHash}` : "";
    if (!usable) {
      exactToken = "";
      exactReady = false;
      exactLayer.hidden = true;
      exactLayer.removeAttribute("href");
      setPrecision("approximate");
      return;
    }
    exactLayer.setAttribute("x", "0");
    exactLayer.setAttribute("y", "0");
    exactLayer.setAttribute("width", String(scene.board.widthMm));
    exactLayer.setAttribute("height", String(scene.board.heightMm));
    if (token === exactToken && exactReady) return;
    exactToken = token;
    exactReady = false;
    exactLayer.hidden = false;
    exactLayer.classList.add("is-loading");
    setPrecision("approximate");
    const url = exactUrl(scene);
    exactLayer.setAttribute("href", url);
    exactLayer.setAttributeNS("http://www.w3.org/1999/xlink", "href", url);
  }

  exactLayer.addEventListener("load", () => {
    if (!scene || exactLayer.getAttribute("href") !== exactUrl(scene)) return;
    exactReady = true;
    exactLayer.classList.remove("is-loading");
    setPrecision("exact");
  });
  exactLayer.addEventListener("error", () => {
    exactReady = false;
    exactLayer.hidden = true;
    exactLayer.classList.remove("is-loading");
    setPrecision("approximate");
    onToast(t("board.exactUnavailable"), "warning");
  });

  function renderReferenceGrid() {
    if (!scene) {
      gridGroup.replaceChildren();
      rulerGroup.replaceChildren();
      return;
    }
    const longest = Math.max(scene.board.widthMm, scene.board.heightMm);
    const step = longest > 600 ? 20 : longest > 250 ? 10 : longest < 50 ? 2.5 : 5;
    const grid = document.createDocumentFragment();
    const rulers = document.createDocumentFragment();
    for (let x = 0; x <= scene.board.widthMm + 0.0001; x += step) {
      grid.append(svg("line", { class: "grid-line", x1: x, y1: 0, x2: x, y2: scene.board.heightMm }));
      if (Math.round(x / step) % 2 === 0) {
        const label = svg("text", { class: "ruler-label", x, y: -0.8 });
        label.textContent = String(x);
        rulers.append(label);
      }
    }
    for (let y = 0; y <= scene.board.heightMm + 0.0001; y += step) {
      grid.append(svg("line", { class: "grid-line", x1: 0, y1: y, x2: scene.board.widthMm, y2: y }));
      if (Math.round(y / step) % 2 === 0) {
        const label = svg("text", { class: "ruler-label", x: -0.8, y, "text-anchor": "end" });
        label.textContent = String(y);
        rulers.append(label);
      }
    }
    gridGroup.replaceChildren(grid);
    rulerGroup.replaceChildren(rulers);
  }

  function fit() {
    if (!scene) return;
    const padding = Math.max(scene.board.widthMm, scene.board.heightMm) * 0.08 + 2;
    camera = { x: -padding, y: -padding, width: scene.board.widthMm + padding * 2, height: scene.board.heightMm + padding * 2 };
    applyCamera();
  }

  function zoom(factor) {
    if (!scene) return;
    const nextWidth = Math.min(scene.board.widthMm * 8, Math.max(scene.board.widthMm * 0.2, camera.width / factor));
    const nextHeight = nextWidth * (camera.height / camera.width);
    const centerX = camera.x + camera.width / 2;
    const centerY = camera.y + camera.height / 2;
    camera = { x: centerX - nextWidth / 2, y: centerY - nextHeight / 2, width: nextWidth, height: nextHeight };
    applyCamera();
  }

  function actualSize() {
    if (!scene) return;
    camera.width = scene.board.widthMm + 10;
    camera.height = scene.board.heightMm + 10;
    camera.x = -5;
    camera.y = -5;
    applyCamera();
  }

  function centerSelection() {
    if (!selection) return fit();
    const point = selection.kind === "route"
      ? { x: (selection.x1 + selection.x2) / 2, y: (selection.y1 + selection.y2) / 2 }
      : selection.bounds
        ? { x: selection.bounds.x + selection.bounds.width / 2, y: selection.bounds.y + selection.bounds.height / 2 }
        : { x: selection.x, y: selection.y };
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return fit();
    camera.x = point.x - camera.width / 2;
    camera.y = point.y - camera.height / 2;
    applyCamera();
  }

  function updateSelection(next, disappeared = false) {
    selection = next;
    const selectedNet = next?.net || (next?.kind === "footprint" ? next.nets?.[0]?.id : next?.kind === "unrouted" ? next.id : "");
    for (const [node, value] of nodes) {
      const isSelected = Boolean(next && value?.key === next.key);
      const sharesNet = Boolean(selectedNet && value && value.net === selectedNet);
      node.classList.toggle("is-selected", isSelected);
      node.classList.toggle("is-dimmed", Boolean(selectedNet && !isSelected && !sharesNet && value?.kind !== "outline"));
    }
    onSelection(selection);
    if (disappeared) onToast("selectionGone", "warning");
  }

  function setObject(node, item) {
    node.dataset.objectKey = item.key;
    node.dataset.objectKind = item.kind;
    nodes.set(node, item);
    objectMap.set(item.key, item);
    for (const [name, value] of [["uuid", item.uuid], ["id", item.id], ["reference", item.reference], ["net", item.net], ["net", item.netName]]) {
      if (value) identityMap.set(`${name}:${String(value)}`, item);
    }
  }

  function render() {
    if (!scene) return;
    objectMap.clear();
    identityMap.clear();
    nodes.clear();
    renderReferenceGrid();
    reconcile(groups.board, [{ key: "board", kind: "outline", x: 0, y: 0, width: scene.board.widthMm, height: scene.board.heightMm }],
      () => svg("rect", { class: "board-fill" }),
      (node, item) => { node.setAttribute("x", item.x); node.setAttribute("y", item.y); node.setAttribute("width", item.width); node.setAttribute("height", item.height); });
    reconcile(groups.outline, scene.outlines,
      () => svg("line", { class: "board-object outline-draw" }),
      (node, item) => { setObject(node, item); node.setAttribute("x1", item.x1); node.setAttribute("y1", item.y1); node.setAttribute("x2", item.x2); node.setAttribute("y2", item.y2); title(node, t("board.outline")); });
    const frontRoutes = scene.routes.filter((item) => !item.layer.startsWith("B."));
    const backRoutes = scene.routes.filter((item) => item.layer.startsWith("B."));
    const drawRoute = (node, item) => { setObject(node, item); node.setAttribute("x1", item.x1); node.setAttribute("y1", item.y1); node.setAttribute("x2", item.x2); node.setAttribute("y2", item.y2); node.setAttribute("stroke-width", item.width); title(node, routeTitle(item)); };
    reconcile(groups.front, frontRoutes, () => svg("line", { class: "board-object route-draw front-route" }), drawRoute);
    reconcile(groups.back, backRoutes, () => svg("line", { class: "board-object route-draw back-route" }), drawRoute);
    reconcile(groups.via, scene.vias,
      () => svg("circle", { class: "board-object via-pop" }),
      (node, item) => { setObject(node, item); node.setAttribute("cx", item.x); node.setAttribute("cy", item.y); node.setAttribute("r", item.diameter / 2); title(node, viaTitle(item)); });
    reconcile(groups.footprint, scene.footprints,
      () => { const group = svg("g", { class: "board-object footprint-draw" }); group.append(svg("rect")); return group; },
      (node, item) => {
        setObject(node, item);
        const rect = node.querySelector("rect");
        const bounds = item.bounds || { x: item.x - 2, y: item.y - 1.5, width: 4, height: 3 };
        rect.setAttribute("x", bounds.x); rect.setAttribute("y", bounds.y); rect.setAttribute("width", bounds.width); rect.setAttribute("height", bounds.height);
        if (item.boundsSource === "native_kicad") node.removeAttribute("transform");
        else node.setAttribute("transform", `rotate(${item.rotation} ${item.x} ${item.y})`);
        node.classList.toggle("is-approximate", item.boundsSource !== "native_kicad");
        title(node, footprintTitle(item));
      });
    reconcile(groups.pad, scene.pads,
      () => svg("rect", { class: "board-object pad-draw" }),
      (node, item) => {
        setObject(node, item);
        node.setAttribute("x", item.x - item.width / 2); node.setAttribute("y", item.y - item.height / 2);
        node.setAttribute("width", item.width); node.setAttribute("height", item.height);
        title(node, padTitle(item));
      });
    reconcile(groups.label, scene.footprints.filter((item) => item.placed),
      () => svg("text", { class: "board-object footprint-label" }),
      (node, item) => { setObject(node, item); node.setAttribute("x", item.x); node.setAttribute("y", item.y - 2.4); node.textContent = item.reference; title(node, footprintTitle(item)); });
    const unroutedLines = scene.unrouted.flatMap((item) => item.endpoints.slice(1).map((endpoint, index) => ({ ...item, key: `${item.key}:${index}`, x1: item.endpoints[0]?.x, y1: item.endpoints[0]?.y, x2: endpoint.x, y2: endpoint.y, net: item.id }))).slice(0, MAX_SCENE_ITEMS);
    reconcile(groups.unrouted, unroutedLines,
      () => svg("line", { class: "board-object unrouted-line" }),
      (node, item) => { setObject(node, item); node.setAttribute("x1", item.x1); node.setAttribute("y1", item.y1); node.setAttribute("x2", item.x2); node.setAttribute("y2", item.y2); title(node, item.name || item.id); });
    groups.finding.replaceChildren();
    updateExactCanvas();
    updateVisibility();
    if (selection && objectMap.has(selection.key)) updateSelection(objectMap.get(selection.key));
    else if (selection) updateSelection(null, true);
    onStatus(scene);
  }

  function setScene(value) {
    const next = normalizeScene(value);
    if (!next) return false;
    const boardChanged = !scene || scene.board.widthMm !== next.board.widthMm || scene.board.heightMm !== next.board.heightMm;
    const bindingChanged = !scene || scene.designRevision !== next.designRevision || scene.contentHash !== next.contentHash;
    scene = next;
    render();
    if (boardChanged || bindingChanged) fit();
    return true;
  }

  function clear() {
    scene = null;
    selection = null;
    objectMap.clear();
    identityMap.clear();
    nodes.clear();
    for (const group of Object.values(groups)) group.replaceChildren();
    gridGroup.replaceChildren();
    rulerGroup.replaceChildren();
    exactToken = "";
    exactReady = false;
    exactLayer.hidden = true;
    exactLayer.removeAttribute("href");
    setPrecision("approximate");
    onSelection(null);
  }

  function locateFinding(finding) {
    if (!scene || !finding || typeof finding !== "object") return false;
    const items = Array.isArray(finding.items) ? finding.items : [];
    for (const item of items) {
      if (!item || typeof item !== "object") continue;
      for (const [name, value] of [["uuid", item.uuid], ["reference", item.reference], ["net", item.net]]) {
        const match = value ? identityMap.get(`${name}:${String(value)}`) : null;
        if (match) {
          updateSelection(match);
          centerSelection();
          return true;
        }
      }
    }
    const rawPosition = items.find((item) => item && typeof item === "object" && item.pos)?.pos || finding.pos;
    const x = Array.isArray(rawPosition) ? number(rawPosition[0], NaN) : number(rawPosition?.x ?? rawPosition?.x_mm, NaN);
    const y = Array.isArray(rawPosition) ? number(rawPosition[1], NaN) : number(rawPosition?.y ?? rawPosition?.y_mm, NaN);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
    const marker = { key: `finding:${text(finding.fingerprint, 128) || `${x}:${y}`}`, kind: "finding", id: text(finding.fingerprint, 128), type: text(finding.type, 160), message: text(finding.message, 500), x, y };
    const node = svg("g", { class: "board-object finding-marker" });
    node.append(svg("circle", { cx: x, cy: y, r: 1.4 }), svg("line", { x1: x - 2.2, y1: y, x2: x + 2.2, y2: y }), svg("line", { x1: x, y1: y - 2.2, x2: x, y2: y + 2.2 }));
    setObject(node, marker);
    title(node, objectSummary(marker, t));
    groups.finding.replaceChildren(node);
    updateSelection(marker);
    centerSelection();
    return true;
  }

  function setPreset(name) {
    const presets = {
      all: { front: true, back: true, vias: true, footprints: true, labels: true, unrouted: true },
      front: { front: true, back: false, vias: true, footprints: true, labels: true, unrouted: true },
      back: { front: false, back: true, vias: true, footprints: true, labels: true, unrouted: true },
      assembly: { front: false, back: false, vias: false, footprints: true, labels: true, unrouted: false },
      routing: { front: true, back: true, vias: true, footprints: false, labels: false, unrouted: true },
    };
    if (!presets[name]) return;
    savePreferences({ ...preferences(), layers: presets[name] });
    updateVisibility();
  }

  function toggleLayer(name) {
    const current = preferences();
    if (!Object.hasOwn(current.layers, name)) return;
    savePreferences({
      ...current,
      layers: { ...current.layers, [name]: !current.layers[name] },
    });
    updateVisibility();
  }

  boardSvg.addEventListener("click", (event) => {
    const node = event.target.closest?.("[data-object-key]");
    if (!node || !boardSvg.contains(node)) return updateSelection(null);
    const value = objectMap.get(node.dataset.objectKey);
    if (value) updateSelection(value);
  });
  boardSvg.addEventListener("pointermove", (event) => {
    if (dragging) {
      const scaleX = camera.width / Math.max(1, boardSvg.clientWidth);
      const scaleY = camera.height / Math.max(1, boardSvg.clientHeight);
      camera.x = dragging.cameraX - (event.clientX - dragging.x) * scaleX;
      camera.y = dragging.cameraY - (event.clientY - dragging.y) * scaleY;
      applyCamera();
      return;
    }
    const node = event.target.closest?.("[data-object-key]");
    const value = node ? objectMap.get(node.dataset.objectKey) : null;
    if (!value) { tooltip.hidden = true; return; }
    tooltip.textContent = objectSummary(value, t);
    tooltip.style.left = `${Math.min(window.innerWidth - 220, event.clientX + 14)}px`;
    tooltip.style.top = `${Math.min(window.innerHeight - 40, event.clientY + 14)}px`;
    tooltip.hidden = false;
  });
  boardSvg.addEventListener("pointerleave", () => { if (!dragging) tooltip.hidden = true; });
  boardSvg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest?.("[data-object-key]")) return;
    dragging = { x: event.clientX, y: event.clientY, cameraX: camera.x, cameraY: camera.y };
    boardSvg.setPointerCapture?.(event.pointerId);
  });
  boardSvg.addEventListener("pointerup", (event) => {
    dragging = null;
    tooltip.hidden = true;
    if (boardSvg.hasPointerCapture?.(event.pointerId)) boardSvg.releasePointerCapture(event.pointerId);
  });
  boardSvg.addEventListener("wheel", (event) => {
    if (!scene) return;
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.16 : 0.86);
  }, { passive: false });

  updateVisibility();
  applyCamera();
  return {
    setScene, clear, fit, actualSize, centerSelection,
    zoomIn: () => zoom(1.22), zoomOut: () => zoom(0.82), setPreset, toggleLayer,
    clearSelection: () => updateSelection(null), getScene: () => scene, getSelection: () => selection,
    locateFinding, refreshLayerVisibility: updateVisibility,
    refreshPrecision: () => setPrecision(exactReady ? "exact" : "approximate"),
  };
}
