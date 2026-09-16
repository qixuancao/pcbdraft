import { createInterface } from "node:readline/promises"
import { stdin as input, stdout as output } from "node:process"
import { GuiClient, type GuiEvent, type Project, type ProjectSession, type TranscriptMessage } from "./bridge.ts"
import { resolveSlashCommand } from "./commands.ts"

const gui = new GuiClient()
const terminal = createInterface({ input, output, prompt: "› " })
const TERMINAL_JOB_EVENTS = new Set(["job.complete", "job.failed"])

let projects: Project[] = []
let current: { project: Project; messages: TranscriptMessage[] } | null = null
let activeMonitor: { jobId: string; controller: AbortController } | null = null

function heading(): void {
  const project = current ? `project: ${current.project.name}` : "project: none"
  output.write(`\nPCBDraft Terminal · ${project}\n`)
  output.write("/resume [text]  projects · /open <number|id>  restore · /stop  cancel · /help · /quit\n")
  output.write("Unique command prefixes work regardless of case, for example /res or /RES.\n\n")
}

function transcript(messages: TranscriptMessage[]): void {
  if (messages.length === 0) {
    output.write("No trusted prior terminal history is associated with this project.\n")
    return
  }
  for (const message of messages) printMessage(message)
}

function printMessage(message: TranscriptMessage): void {
  const label = message.role === "user" ? "You" : "PCBDraft"
  output.write(`${label}: ${message.text}\n\n`)
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
  const selected = /^\d+$/.test(reference) ? projects[Number(reference) - 1] : projects.find((project) => project.id === reference)
  if (!selected) throw new Error("Choose a project number from /resume, or pass its full ID")
  activeMonitor?.controller.abort()
  activeMonitor = null
  const session = await gui.session(selected.id)
  current = { project: selected, messages: session.messages }
  output.write(`\nOpened ${current.project.name} · ${session.status}\n\n`)
  transcript(current.messages)
}

function showLifecycle(event: GuiEvent): void {
  if (event.kind === "job.started") output.write("Working · agent job started\n")
  else if (event.kind === "job.complete") output.write("Finishing · loading the saved response\n")
  else if (event.kind === "job.failed") output.write("Failed · loading the final job state\n")
}

function showNewAssistantMessages(session: ProjectSession): void {
  if (!current) return
  const known = new Set(current.messages.map((message) => message.id))
  const added = session.messages.filter((message) => !known.has(message.id) && message.role === "assistant")
  current.messages = session.messages
  for (const message of added) printMessage(message)
  if (added.length === 0 && session.status === "idle") output.write("Job finished without a new assistant message.\n")
}

async function monitorJob(projectId: string, jobId: string, after: number, controller: AbortController): Promise<void> {
  let cursor = after
  try {
    while (!controller.signal.aborted) {
      let terminalEvent = false
      cursor = await gui.subscribe(projectId, cursor, (event) => {
        showLifecycle(event)
        terminalEvent = TERMINAL_JOB_EVENTS.has(event.kind)
        return !terminalEvent
      }, controller.signal)
      const session = await gui.session(projectId)
      if (current?.project.id === projectId) showNewAssistantMessages(session)
      if (terminalEvent || !session.active_turn) return
      output.write("Connection resumed · the agent is still working\n")
    }
  } catch (error) {
    if (!controller.signal.aborted) output.write(`Event stream error: ${error instanceof Error ? error.message : String(error)}\n`)
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
  output.write(`Queued ${result.job_id} · waiting for lifecycle events (assistant text is shown after it is saved)\n`)
  const controller = new AbortController()
  activeMonitor = { jobId: result.job_id, controller }
  void monitorJob(projectId, result.job_id, snapshot.stream.last_sequence, controller)
}

async function stopCurrent(): Promise<void> {
  if (!current) throw new Error("Open a project first")
  const result = await gui.stop(current.project.id)
  output.write(`Cancellation: ${result.status}\n`)
  if (result.status === "idle" || result.status === "cancelled") {
    activeMonitor?.controller.abort()
    activeMonitor = null
    showNewAssistantMessages(await gui.session(current.project.id))
  }
}

async function main(): Promise<void> {
  heading()
  terminal.prompt()
  try {
    for await (const line of terminal) {
      const trimmed = line.trim()
      const [token = "", ...rest] = trimmed.split(/\s+/)
      const argument = rest.join(" ")
      try {
        if (token.startsWith("/")) {
          const command = resolveSlashCommand(token)
          if (command === "resume") await listProjects(argument)
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
