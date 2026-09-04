export function validateCommandDefinitions(definitions) {
  const ids = new Set();
  const shortcuts = new Set();
  for (const definition of definitions) {
    if (!definition || typeof definition.id !== "string" || !definition.id) {
      throw new Error("command id is invalid");
    }
    if (ids.has(definition.id)) throw new Error("command id is duplicated");
    ids.add(definition.id);
    if (definition.shortcut) {
      const normalized = String(definition.shortcut).toLowerCase();
      if (shortcuts.has(normalized)) throw new Error("command shortcut is duplicated");
      shortcuts.add(normalized);
    }
  }
  return true;
}

export function createCommandRegistry(definitions) {
  validateCommandDefinitions(definitions);
  const commands = definitions.map((item) => ({ ...item }));
  return {
    list(context) {
      return commands.filter((command) => !command.available || command.available(context));
    },
    find(id) {
      return commands.find((command) => command.id === id) || null;
    },
    run(id, context) {
      const command = commands.find((item) => item.id === id);
      if (!command || (command.available && !command.available(context))) return false;
      command.run(context);
      return true;
    },
  };
}

export function createCommandPalette({ dialog, search, list, close, registry, context, t }) {
  let previousFocus = null;
  let active = false;

  function visibleCommands() {
    const query = String(search.value || "").trim().toLocaleLowerCase();
    return registry.list(context()).filter((command) => {
      if (!query) return true;
      return `${t(command.label)} ${command.shortcut || ""}`.toLocaleLowerCase().includes(query);
    });
  }

  function render() {
    const commands = visibleCommands();
    const fragment = document.createDocumentFragment();
    if (!commands.length) {
      const empty = document.createElement("p");
      empty.className = "command-empty";
      empty.textContent = t("command.empty");
      fragment.append(empty);
    }
    for (const command of commands) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "command-item";
      button.dataset.commandId = command.id;
      const label = document.createElement("span");
      label.textContent = t(command.label);
      const shortcut = document.createElement("kbd");
      shortcut.textContent = command.shortcut || "";
      button.append(label, shortcut);
      button.addEventListener("click", () => {
        registry.run(command.id, context());
        hide();
      });
      fragment.append(button);
    }
    list.replaceChildren(fragment);
  }

  function show() {
    if (active) return;
    active = true;
    previousFocus = document.activeElement;
    dialog.hidden = false;
    dialog.setAttribute("aria-hidden", "false");
    search.value = "";
    render();
    window.requestAnimationFrame(() => search.focus());
  }

  function hide() {
    if (!active) return;
    active = false;
    dialog.hidden = true;
    dialog.setAttribute("aria-hidden", "true");
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
  }

  function focusableNodes() {
    return [...dialog.querySelectorAll("button:not([disabled]), input:not([disabled]), [href]")]
      .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true");
  }

  search.addEventListener("input", render);
  close.addEventListener("click", hide);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) hide();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      hide();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = focusableNodes();
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  return { show, hide, toggle: () => (active ? hide() : show()), render, get open() { return active; } };
}
