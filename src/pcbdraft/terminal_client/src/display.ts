import type { GuiEvent, TranscriptMessage } from "./bridge.ts"

export function formatTranscriptMessage(message: TranscriptMessage): string {
  const label = message.role === "user" ? "You" : "PCBDraft"
  return `${label}: ${message.text}\n\n`
}

export function lifecycleMessage(event: GuiEvent): string | null {
  if (event.kind === "job.started") return "Working · agent job started\n"
  if (event.kind === "job.complete") return "Finishing · loading the saved response\n"
  if (event.kind === "job.failed") return "Failed · loading the final job state\n"
  return null
}
