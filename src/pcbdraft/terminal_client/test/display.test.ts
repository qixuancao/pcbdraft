import { expect, test } from "bun:test"
import type { GuiEvent, TranscriptMessage } from "../src/bridge.ts"
import { formatTranscriptMessage, lifecycleMessage } from "../src/display.ts"

function transcriptMessage(role: TranscriptMessage["role"], text: string): TranscriptMessage {
  return { id: "message-1", role, status: "complete", text }
}

function guiEvent(kind: string): GuiEvent {
  return {
    kind,
    message: "",
    level: "info",
    created_at: "2026-09-17T00:00:00Z",
    source: "job",
    sequence: 1,
    stream_id: "stream-1",
  }
}

test("formats saved transcript messages for terminal display", () => {
  expect(formatTranscriptMessage(transcriptMessage("user", "Place R1"))).toBe("You: Place R1\n\n")
  expect(formatTranscriptMessage(transcriptMessage("assistant", "Placed R1"))).toBe("PCBDraft: Placed R1\n\n")
})

test("maps job lifecycle events to terminal status text", () => {
  expect(lifecycleMessage(guiEvent("job.started"))).toBe("Working · agent job started\n")
  expect(lifecycleMessage(guiEvent("job.complete"))).toBe("Finishing · loading the saved response\n")
  expect(lifecycleMessage(guiEvent("job.failed"))).toBe("Failed · loading the final job state\n")
  expect(lifecycleMessage(guiEvent("assistant.delta"))).toBeNull()
})
