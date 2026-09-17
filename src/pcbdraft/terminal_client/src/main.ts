import { createInterface } from "node:readline/promises"
import { stdin as input, stdout as output } from "node:process"
import { GuiClient, type Project, type ProjectSession, type TranscriptMessage } from "./bridge.ts"
import { AssistantPreview } from "./assistant-preview.ts"
import { completeSlashCommand, resolveSlashCommand } from "./commands.ts"
import { formatTranscriptMessage, lifecycleMessage } from "./display.ts"
import { initialProjectId, projectForReference } from "./startup.ts"

const gui = new GuiClient()
const TERMINAL_JOB_EVENTS = new Set(["job.complete", "job.failed"])

let projects: Project[] = []
let current: { project: Project; messages: TranscriptMessage[] } | null = null
let activeMonitor: { jobId: string; controller: AbortController; preview: AssistantPreview } | null = null

function heading(): void {
  const project = current ? `project: ${current.project.name}` : "project: none"
  output.write(`\nPCBDraft Terminal · ${project}\n`)
  output.write("/new <name>  create · /resume [text]  projects · /open <number|id>  restore\n")
  output.write("/stop  cancel · /help · /quit\n")
  output.write("Unique command prefixes work regardless of case, for example /n or /NEW.\n\n")
}

function transcript(messages: TranscriptMessage[]): void {
  if (messages.length === 0) {
    output.write("No trusted prior terminal history is associated with this project.\n")
    return
  }
  for (const message of messages) printMessage(message)
}

function printMessage(message: TranscriptMessage): void {
  output.write(formatTranscriptMessage(message))
}

async function listProjects(query = ""): Promise<void> {
  projects = await gui.projects(query)
  if (projects.length === 0) {
    output.write("No matching PCB projects.\n")
    return
  }
  projects.forEach((project, index) => output.write(`${index + 1}. ${project.name} [${project.status}] ${project.id}\n`))
  output.write("Use /open <number|id>.\n")
}

async function openProject(reference: string): Promise<void> {
  const selected = projectForReference(projects, reference)
  if (!selected) throw new Error("Choose a project number from /resume, or pass its full ID")
  activeMonitor?.controller.abort()
  activeMonitor = null
  const session = await gui.session(selected.id)
  current = { project: selected, messages: session.messages }
  output.write(`\nOpened ${current.project.name} · ${session.status}\n\n`)
  transcript(current.messages)
}

async function createProject(name: string): Promise<void> {
  if (!name.trim()) throw new Error("Usage: /new <name>")
  const project = await gui.createProject(name)
  projects = [project, ...projects.filter((candidate) => candidate.id !== project.id)]
  await openProject(project.id)
}

function showNewAssistantMessages(session: ProjectSession, preview?: AssistantPreview): void {
  if (!current) return
  const known = new Set(current.messages.map((message) => message.id))
  const added = session.messages.filter((message) => !known.has(message.id) && message.role === "assistant")
  current.messages = session.messages
  if (preview) preview.finish(added, session.status)
  else {
    for (const message of added) printMessage(message)
    if (added.length === 0 && session.status === "idle") output.write("Job finished without a new assistant message.\n")
  }
}

async function monitorJob(
  projectId: string,
  jobId: string,
  after: number,
  controller: AbortController,
  preview: AssistantPreview,
): Promise<void> {
  let cursor = after
  try {
    while (!controller.signal.aborted) {
      let terminalEvent = false
      try {
        cursor = await gui.subscribe(projectId, cursor, (event) => {
          cursor = Math.max(cursor, event.sequence)
          if (!preview.consume(event)) {
            const message = lifecycleMessage(event)
            if (message) preview.status(message)
          }
          terminalEvent = TERMINAL_JOB_EVENTS.has(event.kind)
          return !terminalEvent
        }, controller.signal)
      } catch (error) {
        if (controller.signal.aborted) return
        preview.status(`Event stream error: ${error instanceof Error ? error.message : String(error)}\n`)
      }
      if (controller.signal.aborted) return
      try {
        const session = await gui.session(projectId)
        if (terminalEvent || !session.active_turn) {
          if (current?.project.id === projectId) showNewAssistantMessages(session, preview)
          return
        }
        preview.status("Connection resumed · the agent is still working\n")
      } catch (error) {
        preview.status(`Session refresh error: ${error instanceof Error ? error.message : String(error)}\n`)
      }
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
  } finally {
    if (activeMonitor?.jobId === jobId) activeMonitor = null
  }
}

async function sendMessage(text: string): Promise<void> {
  if (!current) throw new Error("Open a project first")
  if (activeMonitor) throw new Error("A project turn is already running; use /stop before sending another message")
  const projectId = current.project.id
  const snapshot = await gui.snapshot(projectId)
  const result = await gui.sendMessage(projectId, text)
  output.write(`Queued ${result.job_id} · streaming the assistant response\n`)
  const controller = new AbortController()
  const preview = new AssistantPreview(result.turn_id, (value) => output.write(value))
  activeMonitor = { jobId: result.job_id, controller, preview }
  void monitorJob(projectId, result.job_id, snapshot.stream.last_sequence, controller, preview)
}

async function stopCurrent(): Promise<void> {
  if (!current) throw new Error("Open a project first")
  const result = await gui.stop(current.project.id)
  activeMonitor?.preview.status(`Cancellation: ${result.status}\n`)
  if (!activeMonitor) output.write(`Cancellation: ${result.status}\n`)
  if (result.status === "idle" || result.status === "cancelled") {
    const monitor = activeMonitor
    monitor?.controller.abort()
    activeMonitor = null
    showNewAssistantMessages(await gui.session(current.project.id), monitor?.preview)
  }
}

async function main(): Promise<void> {
  heading()
  const startupProject = initialProjectId()
  if (startupProject) {
    try {
      projects = await gui.projects()
      await openProject(startupProject)
    } catch (error) {
      output.write(`Error opening initial project: ${error instanceof Error ? error.message : String(error)}\n`)
    }
  }
  const terminal = createInterface({ input, output, prompt: "› ", completer: completeSlashCommand })
  terminal.prompt()
  try {
    for await (const line of terminal) {
      const trimmed = line.trim()
      const [token = "", ...rest] = trimmed.split(/\s+/)
      const argument = rest.join(" ")
      try {
        if (token.startsWith("/")) {
          const command = resolveSlashCommand(token)
          if (command === "new") await createProject(argument)
          else if (command === "resume") await listProjects(argument)
          else if (command === "open") await openProject(argument)
          else if (command === "help") heading()
          else if (command === "stop") await stopCurrent()
          else if (command === "quit" || command === "exit") break
        } else if (trimmed) await sendMessage(line)
      } catch (error) {
        output.write(`Error: ${error instanceof Error ? error.message : String(error)}\n`)
      }
      terminal.prompt()
    }
  } finally {
    activeMonitor?.controller.abort()
    terminal.close()
    output.write("\n")
  }
}

await main()
