export const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

function clippedText(value, limit = 500) {
  if (typeof value !== "string") return "";
  const clean = value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, " ").trim();
  return clean.length > limit ? `${clean.slice(0, Math.max(0, limit - 1))}…` : clean;
}

export function createApi({ appBase = new URL(".", document.baseURI), csrfToken = "" } = {}) {
  const base = appBase instanceof URL ? appBase : new URL(".", document.baseURI);
  const apiBase = new URL("api/", base);
  let csrf = csrfToken;

  function url(value) {
    return new URL(String(value || "").replace(/^\/+/, ""), apiBase);
  }

  async function request(value, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const requestOptions = {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    };
    if (Object.prototype.hasOwnProperty.call(options, "body")) {
      headers.set("Content-Type", "application/json");
      if (csrf) headers.set("X-PCBDraft-CSRF", csrf);
      requestOptions.body = JSON.stringify(options.body);
    }
    const response = await window.fetch(url(value), requestOptions);
    const advertised = Number(response.headers.get("content-length") || 0);
    if (advertised > MAX_RESPONSE_BYTES) throw new Error("GUI response exceeded the bounded client limit");
    const raw = await response.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_RESPONSE_BYTES) {
      throw new Error("GUI response exceeded the bounded client limit");
    }
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch (_error) {
        throw new Error("GUI endpoint returned invalid JSON");
      }
    }
    if (!response.ok) {
      const message = payload?.error?.message || payload?.detail || payload?.message || `Request failed (${response.status})`;
      const error = new Error(clippedText(String(message)) || "Request failed");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function projectPath(projectId, suffix = "") {
    const id = encodeURIComponent(String(projectId || ""));
    const tail = String(suffix || "").replace(/^\/+/, "");
    return `projects/${id}${tail ? `/${tail}` : ""}`;
  }

  return {
    base,
    apiBase,
    url,
    projectPath,
    setCsrfToken(value) {
      csrf = typeof value === "string" ? value : "";
    },
    get(value, options) {
      return request(value, { ...options, method: "GET" });
    },
    post(value, body = {}, options) {
      return request(value, { ...options, method: "POST", body });
    },
  };
}
