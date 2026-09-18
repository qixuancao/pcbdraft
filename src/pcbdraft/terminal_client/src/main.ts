import { createInterface } from "node:readline/promises"
import { stdin as input, stdout as output } from "node:process"
import { GuiClient, type GuiEvent, type Project, type ProjectSession, type TranscriptMessage } from "./bridge.ts"
import { completeSlashCommand, resolveSlashCommand } from "./commands.ts"
import { lifecycleMessage } from "./display.ts"
import { OpenTuiApp, type OpenTuiState } from "./opentui-app.ts"
import { DEFAULT_TUI_COMMANDS, type TuiStatus } from "./tui.ts"
import { initialProjectId, projectForReference } from "./startup.ts"

const gui = new GuiClient()
const TERMINAL_JOB_EVENTS = new Set(["job.complete", "job.failed"])
const FULLSCREEN_TUI = Boolean(input.isTTY && output.isTTY && process.env.TERM !== "dumb")

let projects: Project[] = []
let current: { project: Project; messages: TranscriptMessage[] } | null = null
let activeMonitor: {
  jobId: string
  turnId: string
  controller: AbortController
} | null = null
let preview = ""
let status: TuiStatus = { text: "Ready", tone: "idle" }
let notices: string[] = []
let tui: OpenTuiApp | null = null
let quitRequested = false
let finishTui: (() => void) | null = null

function setStatus(text: string, tone: TuiStatus["tone"] = "idle"): void {
  status = { text: text.replaceAll(/\s+/g, " ").trim(), tone }
  refreshTui()
}

function addNotice(text: string): void {
  const cleaned = text.replaceAll(/\s+/g, " ").trim()
  if (!cleaned) return
  notices = [...notices, cleaned].slice(-8)
  refreshTui()
}

function refreshTui(): void {
  if (!tui) return
  const state: OpenTuiState = {
    title: "PCBDraft",
    project: current ? { name: current.project.name, status: current.project.status } : null,
    messages: current?.messages ?? [],
    preview: preview || undefined,
    status,
    commands: DEFAULT_TUI_COMMANDS,
    footer: current?.project.id,
    notices,
  }
  tui.update(state)
}

function legacyWrite(text: string): void {
  if (FULLSCREEN_TUI) return
  output.write(text)
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function reportError(error: unknown): void {
  const message = `Error: ${errorMessage(error)}`
  if (FULLSCREEN_TUI) {
    addNotice(message)
    setStatus(message, "error")
  } else {
    legacyWrite(`${message}\n`)
  }
}

function transcript(messages: TranscriptMessage[]): void {
  if (messages.length === 0) {
    legacyWrite("No trusted prior terminal history is associated with this project.\n")
    return
  }
  if (FULLSCREEN_TUI) return
  for (const message of messages) {
    const label = message.role === "user" ? "You" : "PCBDraft"
    legacyWrite(`${label}: ${message.text}\n\n`)
  }
}

function heading(): void {
  if (FULLSCREEN_TUI) return
  legacyWrite("\nPCBDraft Terminal\n")
  legacyWrite("/new <name>  create · /resume [text]  projects · /open <number|id>  restore\n")
  legacyWrite("/stop  cancel · /help · /quit\n")
  legacyWrite("Unique command prefixes work regardless of case, for example /n or /NEW.\n\n")
}

async function listProjects(query = ""): Promise<void> {
  projects = await gui.projects(query)
  if (projects.length === 0) {
    const message = "No matching PCB projects."
    if (FULLSCREEN_TUI) {
      addNotice(message)
      setStatus(message, "idle")
    } else {
      legacyWrite(`${message}\n`)
    }
    return
  }
  const lines = projects.map((project, index) => `${index + 1}. ${project.name} [${project.status}] ${project.id}`)
  if (FULLSCREEN_TUI) {
    notices = [...lines, "Use /open <number|id> to restore a project."].slice(-8)
    setStatus(`${projects.length} project${projects.length === 1 ? "" : "s"} found`, "success")
  } else {
    lines.forEach((line) => legacyWrite(`${line}\n`))
    legacyWrite("Use /open <number|id>.\n")
  }
}

async function startMonitor(projectId: string, jobId: string, turnId: string, after: number): Promise<void> {
  activeMonitor?.controller.abort()
  const controller = new AbortController()
  activeMonitor = { jobId, turnId, controller }
  preview = ""
  setStatus("Working · agent response streaming", "working")
  void monitorJob(projectId, jobId, turnId, after, controller)
}

async function openProject(reference: string): Promise<void> {
  let selected = projectForReference(projects, reference)
  if (!selected) {
    // `/open <id>` is valid after a cold start as well as after `/resume`.
    projects = await gui.projects()
    selected = projectForReference(projects, reference)
  }
  if (!selected) throw new Error("Choose a project number from /resume, or pass its full ID")

  activeMonitor?.controller.abort()
  activeMonitor = null
  preview = ""
  const session = await gui.session(selected.id)
  current = { project: selected, messages: session.messages }
  notices = session.messages.length === 0 ? ["No trusted prior terminal history is associated with this project."] : []
  setStatus(`Opened ${selected.name} · ${session.status}`, session.status === "idle" ? "success" : "working")
  transcript(session.messages)
  refreshTui()

  if (session.active_turn) {
    const snapshot = await gui.snapshot(selected.id)
    await startMonitor(selected.id, session.active_turn.job_id, session.active_turn.turn_id, snapshot.stream.last_sequence)
  }
}

async function createProject(name: string): Promise<void> {
  if (!name.trim()) throw new Error("Usage: /new <name>")
  const project = await gui.createProject(name)
  projects = [project, ...projects.filter((candidate) => candidate.id !== project.id)]
  await openProject(project.id)
}

function updateSession(session: ProjectSession, terminalStatus?: string): void {
  if (!current || session.project_id !== current.project.id) return
  current.messages = session.messages
  preview = ""
  if (terminalStatus === "idle") setStatus("Ready · response saved", "success")
  else if (terminalStatus) setStatus(terminalStatus, "error")
  refreshTui()
}

async function monitorJob(
  projectId: string,
  jobId: string,
  turnId: string,
  after: number,
  controller: AbortController,
): Promise<void> {
  let cursor = after
  try {
    while (!controller.signal.aborted) {
      let terminalEvent = false
      try {
        cursor = await gui.subscribe(projectId, cursor, (event: GuiEvent) => {
          cursor = Math.max(cursor, event.sequence)
          if (event.kind === "assistant.delta" && event.turn_id === turnId && event.text) {
            preview += event.text
            refreshTui()
          } else {
            const message = lifecycleMessage(event)
            if (message) setStatus(message, event.kind === "job.failed" ? "error" : "working")
          }
          terminalEvent = TERMINAL_JOB_EVENTS.has(event.kind)
          return !terminalEvent
        }, controller.signal)
      } catch (error) {
        if (controller.signal.aborted) return
        setStatus(`Connection error · ${errorMessage(error)}`, "error")
      }
      if (controller.signal.aborted) return
      try {
        const session = await gui.session(projectId)
        if (terminalEvent || !session.active_turn) {
          updateSession(session, session.status)
          return
        }
        setStatus("Connection resumed · agent is still working", "working")
      } catch (error) {
        setStatus(`Session refresh error · ${errorMessage(error)}`, "error")
      }
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
  } finally {
    if (activeMonitor?.jobId === jobId) activeMonitor = null
    if (!controller.signal.aborted && current?.project.id === projectId) setStatus("Ready", "idle")
  }
}

async function sendMessage(text: string): Promise<void> {
  if (!current) throw new Error("Open a project first")
  if (activeMonitor) throw new Error("A project turn is already running; use Esc or /stop before sending another message")
  const projectId = current.project.id
  const snapshot = await gui.snapshot(projectId)
  const result = await gui.sendMessage(projectId, text)
  current.messages = [
    ...current.messages,
    { id: `pending-${result.turn_id}`, role: "user", status: "pending", text },
  ]
  if (FULLSCREEN_TUI) setStatus("Queued · streaming the assistant response", "working")
  else legacyWrite(`Queued ${result.job_id} · streaming the assistant response\n`)
  await startMonitor(projectId, result.job_id, result.turn_id, snapshot.stream.last_sequence)
}

async function stopCurrent(): Promise<void> {
  if (!current) throw new Error("Open a project first")
  const result = await gui.stop(current.project.id)
  activeMonitor?.controller.abort()
  activeMonitor = null
  preview = ""
  const session = await gui.session(current.project.id)
  updateSession(session, session.status)
  setStatus(`Cancellation: ${result.status}`, result.status === "cancelled" || result.status === "idle" ? "success" : "error")
  if (!FULLSCREEN_TUI) legacyWrite(`Cancellation: ${result.status}\n`)
}

async function handleLine(line: string): Promise<void> {
  const trimmed = line.trim()
  if (!trimmed) return
  const [token = "", ...rest] = trimmed.split(/\s+/)
  const argument = rest.join(" ")
  try {
    if (token.startsWith("/")) {
      const command = resolveSlashCommand(token)
      if (command === "new") await createProject(argument)
      else if (command === "resume") await listProjects(argument)
      else if (command === "open") await openProject(argument)
      else if (command === "help") {
        if (FULLSCREEN_TUI) {
          notices = [
            "/new <name>  create a project",
            "/resume [text]  list projects",
            "/open <number|id>  restore a project",
            "/stop  cancel the current turn",
            "/quit  exit PCBDraft",
            "Unique command prefixes are case-insensitive; Tab completes a unique prefix.",
          ]
          setStatus("Help", "idle")
        } else heading()
      } else if (command === "stop") await stopCurrent()
      else if (command === "quit" || command === "exit") {
        quitRequested = true
        finishTui?.()
      }
    } else await sendMessage(line)
  } catch (error) {
    reportError(error)
  }
}

async function runLineMode(): Promise<void> {
  heading()
  const startupProject = initialProjectId()
  if (startupProject) {
    try {
      projects = await gui.projects()
      await openProject(startupProject)
    } catch (error) {
      reportError(error)
    }
  }
  const terminal = createInterface({ input, output, prompt: "› ", completer: completeSlashCommand })
  terminal.prompt()
  try {
    for await (const line of terminal) {
      await handleLine(line)
      if (quitRequested) break
      terminal.prompt()
    }
  } finally {
    activeMonitor?.controller.abort()
    terminal.close()
    legacyWrite("\n")
  }
}

async function runFullscreenTui(): Promise<void> {
  let resolveDone: (() => void) | undefined
  const done = new Promise<void>((resolve) => { resolveDone = resolve })
  finishTui = () => resolveDone?.()
  tui = new OpenTuiApp({
    submit: (text) => handleLine(text),
    stop: () => stopCurrent().catch(reportError),
    quit: () => {
      quitRequested = true
      resolveDone?.()
    },
  })
  await tui.start({
    title: "PCBDraft",
    project: null,
    messages: [],
    status,
    commands: DEFAULT_TUI_COMMANDS,
    footer: "Type a message or /help",
  })
  const startupProject = initialProjectId()
  if (startupProject) {
    try {
      projects = await gui.projects()
      await openProject(startupProject)
    } catch (error) {
      reportError(error)
    }
  }
  await done
  finishTui = null
  activeMonitor?.controller.abort()
  activeMonitor = null
  tui.close()
  tui = null
}

if (FULLSCREEN_TUI) await runFullscreenTui()
else await runLineMode()
