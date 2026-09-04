const MAX_CHAT_MESSAGES = 100;
const MAX_ACTIVITY_ITEMS = 120;

function clean(value, limit = 16_000) {
  if (typeof value !== "string") return "";
  const text = value.replace(/\u0000/g, "");
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}

function safeLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch (_error) {
    return null;
  }
}

async function copy(value, onToast, t) {
  if (!value || !navigator.clipboard?.writeText) return;
  try {
    await navigator.clipboard.writeText(value);
    onToast(t("chat.copied"), "info");
  } catch (_error) {
    onToast(t("state.error"), "error");
  }
}

function appendInline(parent, value) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|https?:\/\/[^\s<]+)/g;
  let cursor = 0;
  for (const match of value.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(value.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else {
      const url = safeLink(token);
      if (url) {
        const link = document.createElement("a");
        link.href = url.toString();
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = url.toString();
        parent.append(link);
      } else {
        parent.append(document.createTextNode(token));
      }
    }
    cursor = match.index + token.length;
  }
  if (cursor < value.length) parent.append(document.createTextNode(value.slice(cursor)));
}

function richText(value, onToast, t) {
  const fragment = document.createDocumentFragment();
  const parts = clean(value, 64 * 1024).split("```");
  for (let index = 0; index < parts.length; index += 1) {
    const block = parts[index];
    if (index % 2 === 1) {
      const shell = document.createElement("div");
      shell.className = "message-code";
      const code = document.createElement("code");
      code.textContent = block.replace(/^\w+\n/, "");
      const action = document.createElement("button");
      action.type = "button";
      action.className = "code-copy";
      action.textContent = t("chat.copy");
      action.addEventListener("click", () => copy(code.textContent || "", onToast, t));
      shell.append(action, code);
      fragment.append(shell);
      continue;
    }
    const lines = block.split(/\r?\n/);
    let list = null;
    for (const rawLine of lines) {
      const heading = rawLine.match(/^(#{1,3})\s+(.+)$/);
      const bullet = rawLine.match(/^[-*]\s+(.+)$/);
      if (heading) {
        list = null;
        const node = document.createElement(`h${heading[1].length + 2}`);
        appendInline(node, heading[2]);
        fragment.append(node);
      } else if (bullet) {
        if (!list) {
          list = document.createElement("ul");
          fragment.append(list);
        }
        const item = document.createElement("li");
        appendInline(item, bullet[1]);
        list.append(item);
      } else if (rawLine.trim()) {
        list = null;
        const paragraph = document.createElement("p");
        appendInline(paragraph, rawLine);
        fragment.append(paragraph);
      }
    }
  }
  return fragment;
}

function normalizeSession(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const messages = Array.isArray(source.messages) ? source.messages : [];
  return {
    status: clean(source.status, 32) || "idle",
    messages: messages.slice(-MAX_CHAT_MESSAGES).flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const role = item.role === "user" ? "user" : item.role === "assistant" ? "assistant" : "system";
      const body = clean(item.text, 64 * 1024);
      return body ? [{ id: clean(item.id, 128), role, text: body, status: clean(item.status, 32), createdAt: clean(item.created_at, 64) }] : [];
    }),
  };
}

function normalizeEvent(value) {
  if (!value || typeof value !== "object") return null;
  const kind = clean(value.kind, 96);
  const message = clean(value.message, 512);
  if (!kind || !message) return null;
  return {
    id: Number.isInteger(value.sequence) ? String(value.sequence) : `${kind}-${clean(value.created_at, 64)}`,
    kind, message, level: clean(value.level, 32), source: clean(value.source, 32), createdAt: clean(value.created_at, 64),
  };
}

function localizedState(value, t) {
  const key = `state.${value || "idle"}`;
  const translated = t(key);
  return translated === key ? clean(value, 32) || t("state.idle") : translated;
}

export function createConversation({ elements, api, t, onToast }) {
  let projectId = "";
  let session = normalizeSession({});
  let events = [];
  let projectVersion = 0;
  let loadVersion = 0;
  let sendPending = false;

  function isActive() {
    return ["queued", "running", "cancel_requested", "starting", "stopping"].includes(session.status);
  }

  function renderMessages() {
    const fragment = document.createDocumentFragment();
    for (const message of session.messages) {
      const card = document.createElement("article");
      card.className = `message message-${message.role}`;
      const header = document.createElement("header");
      const role = document.createElement("strong");
      const detail = document.createElement("span");
      const action = document.createElement("button");
      role.textContent = message.role === "user" ? t("chat.you") : t("chat.agent");
      detail.textContent = [message.status, message.createdAt].filter(Boolean).join(" · ");
      action.type = "button";
      action.className = "message-copy";
      action.textContent = t("chat.copy");
      action.addEventListener("click", () => copy(message.text, onToast, t));
      header.append(role, detail, action);
      const body = document.createElement("div");
      body.className = "message-body";
      body.append(richText(message.text, onToast, t));
      card.append(header, body);
      fragment.append(card);
    }
    elements.chatLog.replaceChildren(fragment);
    elements.chatEmpty.hidden = session.messages.length > 0;
    elements.turnState.textContent = localizedState(session.status, t);
    const active = isActive();
    elements.stop.disabled = !projectId || !active;
    elements.send.disabled = !projectId || active || sendPending;
    elements.input.disabled = !projectId;
  }

  function renderActivity() {
    const kindFilter = elements.activityType.value || "all";
    const stateFilter = elements.activityState.value || "all";
    const filtered = events.filter((event) => {
      const broadKind = event.kind.startsWith("tool") ? "tool" : event.kind.startsWith("turn") || event.kind.startsWith("model") ? "agent" : "pcb";
      return (kindFilter === "all" || kindFilter === broadKind) && (stateFilter === "all" || event.level === stateFilter);
    });
    const fragment = document.createDocumentFragment();
    for (const event of filtered.slice(-MAX_ACTIVITY_ITEMS).reverse()) {
      const item = document.createElement("li");
      item.className = `activity-item activity-${event.level || "info"}`;
      const title = document.createElement("strong");
      const detail = document.createElement("span");
      title.textContent = event.message;
      detail.textContent = [event.kind, event.createdAt].filter(Boolean).join(" · ");
      item.append(title, detail);
      fragment.append(item);
    }
    elements.activityList.replaceChildren(fragment);
    elements.activityEmpty.hidden = filtered.length > 0;
  }

  async function refreshSession() {
    if (!projectId) return;
    const version = ++loadVersion;
    const selected = projectId;
    try {
      const payload = await api.get(api.projectPath(selected, "session"));
      if (version !== loadVersion || selected !== projectId) return;
      session = normalizeSession(payload);
      renderMessages();
    } catch (_error) {
      if (version === loadVersion) onToast(t("state.error"), "error");
    }
  }

  async function send(event) {
    event.preventDefault();
    const value = clean(elements.input.value, 16 * 1024);
    if (!projectId || !value || isActive() || sendPending) return;
    const selected = projectId;
    const version = projectVersion;
    sendPending = true;
    elements.send.disabled = true;
    try {
      await api.post(api.projectPath(selected, "messages"), { text: value });
      if (version !== projectVersion) return;
      if (elements.input.value === value) elements.input.value = "";
      await refreshSession();
    } catch (_error) {
      if (version === projectVersion) onToast(t("state.error"), "error");
    } finally {
      if (version === projectVersion) {
        sendPending = false;
        renderMessages();
      }
    }
  }

  async function stop() {
    if (!projectId) return;
    const selected = projectId;
    const version = projectVersion;
    try {
      await api.post(api.projectPath(selected, "stop"), {});
      if (version !== projectVersion) return;
      await refreshSession();
    } catch (_error) {
      if (version === projectVersion) onToast(t("state.error"), "error");
    }
  }

  for (const button of elements.tabs.querySelectorAll("[role='tab']")) {
    button.addEventListener("click", () => {
      const target = button.dataset.drawerTab;
      for (const tab of elements.tabs.querySelectorAll("[role='tab']")) tab.setAttribute("aria-selected", String(tab === button));
      elements.conversationPane.hidden = target !== "conversation";
      elements.activityPane.hidden = target !== "activity";
    });
  }
  elements.form.addEventListener("submit", send);
  elements.input.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") send(event);
    if (event.key === "Escape") elements.input.blur();
  });
  elements.stop.addEventListener("click", stop);
  elements.activityType.addEventListener("change", renderActivity);
  elements.activityState.addEventListener("change", renderActivity);

  return {
    setProject(id) {
      projectVersion += 1;
      loadVersion += 1;
      sendPending = false;
      projectId = typeof id === "string" ? id : "";
      session = normalizeSession({});
      events = [];
      renderMessages();
      renderActivity();
      return refreshSession();
    },
    setSession(payload) {
      loadVersion += 1;
      session = normalizeSession(payload);
      renderMessages();
    },
    addEvent(payload) {
      const event = normalizeEvent(payload);
      if (!event || events.some((item) => item.id === event.id)) return;
      events = [...events, event].slice(-MAX_ACTIVITY_ITEMS);
      renderActivity();
    },
    refreshSession,
    refreshLabels() {
      renderMessages(); renderActivity();
    },
  };
}
