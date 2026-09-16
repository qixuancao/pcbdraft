import { createInterface } from "node:readline/promises"
import { stdin as input, stdout as output } from "node:process"
import { GuiClient, type Project, type TranscriptMessage } from "./bridge.ts"

const gui = new GuiClient()
const terminal = createInterface({ input, output, prompt: "› " })

let projects: Project[] = []
let current: { project: Project; messages: TranscriptMessage[] } | null = null

function heading(): void {
  const project = current ? `project: ${current.project.name}` : "project: none"
  output.write(`\nPCBDraft TypeScript TUI prototype · ${project}\n`)
  output.write("/resume [text]  list projects · /open <number|id>  restore · /stop  cancel · /help\n\n")
}

function transcript(messages: TranscriptMessage[]): void {
  if (messages.length === 0) {
    output.write("No trusted prior terminal history is associated with this project.\n")
    return
  }
  for (const message of messages) {
    const label = message.role === "user" ? "You" : "PCBDraft"
    output.write(`${label}: ${message.text}\n\n`)
  }
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
  const session = await gui.session(selected.id)
  current = { project: selected, messages: session.messages }
  output.write(`\nOpened ${current.project.name} · ${session.status}\n\n`)
  transcript(current.messages)
}

async function main(): Promise<void> {
  heading()
  terminal.prompt()
  try {
    for await (const line of terminal) {
      const [command, ...rest] = line.trim().split(/\s+/)
      const argument = rest.join(" ")
      try {
        if (command === "/resume") await listProjects(argument)
        else if (command === "/open") await openProject(argument)
        else if (command === "/help" || command === "?") heading()
        else if (command === "/stop") {
          if (!current) throw new Error("Open a project first")
          const result = await gui.stop(current.project.id)
          output.write(`Cancellation: ${result.status}\n`)
        } else if (command === "/quit" || command === "/exit") break
        else if (line.trim()) {
          if (!current) throw new Error("Open a project first")
          const result = await gui.sendMessage(current.project.id, line)
          output.write(`Queued ${result.job_id}. Use /open ${current.project.id} to refresh history.\n`)
        }
      } catch (error) {
        output.write(`Error: ${error instanceof Error ? error.message : String(error)}\n`)
      }
      terminal.prompt()
    }
  } finally {
    terminal.close()
    output.write("\n")
  }
}

await main()
