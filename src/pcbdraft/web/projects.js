const STATUS_GROUPS = {
  generating: new Set(["draft", "generating", "validating", "releasing", "interrupted"]),
  ready: new Set(["generated", "validated", "released"]),
};

function clean(value, limit = 160) {
  if (typeof value !== "string") return "";
  const text = value.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function normalizeProjects(value) {
  const rows = Array.isArray(value) ? value : value?.projects;
  if (!Array.isArray(rows)) return [];
  return rows.slice(0, 500).flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const id = clean(row.id || row.project_id, 80);
    if (!id) return [];
    return [{
      id,
      name: clean(row.name || row.title || id),
      status: clean(row.status || "unknown", 64),
      updatedAt: clean(row.updated_at, 64),
      designRevision: Number.isInteger(row.design_revision) ? row.design_revision : 0,
    }];
  });
}

function statusGroup(status) {
  if (STATUS_GROUPS.generating.has(status)) return "generating";
  if (STATUS_GROUPS.ready.has(status)) return "ready";
  return "attention";
}

function localizedStatus(status, t) {
  const key = `projectStatus.${status || "unknown"}`;
  const translated = t(key);
  return translated === key ? status || t("projectStatus.unknown") : translated;
}

function projectButton(project, selected, onOpen, t) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "project-row";
  button.dataset.projectId = project.id;
  button.setAttribute("role", "option");
  button.setAttribute("aria-selected", String(project.id === selected));
  const title = document.createElement("span");
  title.className = "project-row-title";
  title.textContent = project.name;
  const detail = document.createElement("span");
  detail.className = "project-row-detail";
  detail.textContent = `${localizedStatus(project.status, t)} · r${project.designRevision}`;
  button.append(title, detail);
  button.addEventListener("click", () => onOpen(project.id));
  return button;
}

export function createProjectRail({ search, status, list, recent, onOpen, t }) {
  let projects = [];
  let selected = "";
  let remembered = [];
  let cursor = -1;

  function matching() {
    const query = clean(search.value, 160).toLocaleLowerCase();
    const filter = status.value || "all";
    return projects.filter((project) => {
      if (filter !== "all" && statusGroup(project.status) !== filter) return false;
      return !query || `${project.name} ${project.id}`.toLocaleLowerCase().includes(query);
    });
  }

  function renderRecent() {
    const candidates = remembered
      .map((id) => projects.find((project) => project.id === id))
      .filter(Boolean)
      .slice(0, 6);
    const fragment = document.createDocumentFragment();
    for (const project of candidates) fragment.append(projectButton(project, selected, onOpen, t));
    recent.replaceChildren(fragment);
    recent.hidden = candidates.length === 0;
  }

  function render() {
    const candidates = matching();
    if (cursor >= candidates.length) cursor = candidates.length - 1;
    const fragment = document.createDocumentFragment();
    if (!candidates.length) {
      const empty = document.createElement("p");
      empty.className = "rail-empty";
      empty.textContent = t("rail.empty");
      fragment.append(empty);
    }
    for (const project of candidates) fragment.append(projectButton(project, selected, onOpen, t));
    list.replaceChildren(fragment);
    renderRecent();
  }

  function openCursor() {
    const candidates = matching();
    const project = candidates[cursor];
    if (project) onOpen(project.id);
  }

  search.addEventListener("input", () => {
    cursor = -1;
    render();
  });
  status.addEventListener("change", () => {
    cursor = -1;
    render();
  });
  search.addEventListener("keydown", (event) => {
    const candidates = matching();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      cursor = Math.min(candidates.length - 1, cursor + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      cursor = Math.max(0, cursor - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      openCursor();
      return;
    } else {
      return;
    }
    for (const button of list.querySelectorAll(".project-row")) {
      button.classList.toggle("keyboard-current", button.dataset.projectId === candidates[cursor]?.id);
    }
  });

  return {
    setProjects(value) {
      projects = normalizeProjects(value);
      render();
      return projects;
    },
    setSelected(projectId) {
      selected = typeof projectId === "string" ? projectId : "";
      render();
    },
    setRemembered(ids) {
      remembered = Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
      renderRecent();
    },
    getProjects() {
      return [...projects];
    },
    refreshLabels() {
      render();
    },
  };
}
