export const PREFERENCE_SCHEMA_VERSION = 1;
export const PREFERENCE_STORAGE_KEY = "pcbdraft.workbench.preferences";
export const ALLOWED_PREFERENCE_KEYS = Object.freeze([
  "version",
  "theme",
  "locale",
  "density",
  "railWidth",
  "inspectorWidth",
  "drawerHeight",
  "panels",
  "layers",
  "recentProjects",
]);

const PROJECT_ID = /^[a-z][a-z0-9-]{2,79}$/;
const THEMES = new Set(["dark", "light", "system"]);
const LOCALES = new Set(["zh-CN", "en"]);
const DENSITIES = new Set(["comfortable", "compact"]);
const PANEL_KEYS = new Set(["railCollapsed", "inspectorCollapsed", "drawerOpen"]);
const LAYER_KEYS = new Set(["front", "back", "vias", "footprints", "labels", "unrouted"]);

function clamp(value, minimum, maximum, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(number)));
}

function safeStorage() {
  try {
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

export function defaultPreferences(language = navigator.language) {
  return {
    version: PREFERENCE_SCHEMA_VERSION,
    theme: "system",
    locale: String(language || "").toLowerCase().startsWith("zh") ? "zh-CN" : "en",
    density: "comfortable",
    railWidth: 280,
    inspectorWidth: 360,
    drawerHeight: 300,
    panels: {
      railCollapsed: false,
      inspectorCollapsed: false,
      drawerOpen: false,
    },
    layers: {
      front: true,
      back: true,
      vias: true,
      footprints: true,
      labels: true,
      unrouted: true,
    },
    recentProjects: [],
  };
}

export function sanitizePreferences(value, language = navigator.language) {
  const fallback = defaultPreferences(language);
  if (!value || typeof value !== "object" || Array.isArray(value)) return fallback;
  if (value.version !== PREFERENCE_SCHEMA_VERSION) return fallback;
  const panels = value.panels && typeof value.panels === "object" ? value.panels : {};
  const layers = value.layers && typeof value.layers === "object" ? value.layers : {};
  const recent = Array.isArray(value.recentProjects) ? value.recentProjects : [];
  return {
    version: PREFERENCE_SCHEMA_VERSION,
    theme: THEMES.has(value.theme) ? value.theme : fallback.theme,
    locale: LOCALES.has(value.locale) ? value.locale : fallback.locale,
    density: DENSITIES.has(value.density) ? value.density : fallback.density,
    railWidth: clamp(value.railWidth, 220, 320, fallback.railWidth),
    inspectorWidth: clamp(value.inspectorWidth, 300, 460, fallback.inspectorWidth),
    drawerHeight: clamp(value.drawerHeight, 180, 720, fallback.drawerHeight),
    panels: Object.fromEntries(
      [...PANEL_KEYS].map((key) => [key, Boolean(panels[key])]),
    ),
    layers: Object.fromEntries(
      [...LAYER_KEYS].map((key) => [key, layers[key] !== false]),
    ),
    recentProjects: [...new Set(recent.filter((id) => typeof id === "string" && PROJECT_ID.test(id)))].slice(0, 12),
  };
}

export function loadPreferences(language = navigator.language) {
  const storage = safeStorage();
  if (!storage) return defaultPreferences(language);
  try {
    return sanitizePreferences(JSON.parse(storage.getItem(PREFERENCE_STORAGE_KEY) || ""), language);
  } catch (_error) {
    return defaultPreferences(language);
  }
}

export function savePreferences(preferences) {
  const storage = safeStorage();
  const safe = sanitizePreferences(preferences);
  if (!storage) return safe;
  try {
    storage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify(safe));
  } catch (_error) {
    // A private-mode quota failure must not make the workbench unusable.
  }
  return safe;
}

export function rememberProject(preferences, projectId) {
  if (typeof projectId !== "string" || !PROJECT_ID.test(projectId)) return preferences;
  return {
    ...preferences,
    recentProjects: [projectId, ...preferences.recentProjects.filter((id) => id !== projectId)].slice(0, 12),
  };
}

export function createPreferenceStore(language = navigator.language) {
  let preferences = loadPreferences(language);
  const listeners = new Set();
  function notify() {
    for (const listener of listeners) listener(preferences);
  }
  return {
    get() {
      return preferences;
    },
    update(patch) {
      preferences = savePreferences({ ...preferences, ...patch });
      notify();
      return preferences;
    },
    replace(next) {
      preferences = savePreferences(next);
      notify();
      return preferences;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
