import { expect, test } from "bun:test"
import { AssistantPreview } from "../src/assistant-preview.ts"
import type { GuiEvent, TranscriptMessage } from "../src/bridge.ts"

function delta(turnId: string, text: string): GuiEvent {
  return {
    kind: "assistant.delta",
    message: "Assistant response update",
    level: "info",
    created_at: "now",
    source: "agent",
    sequence: 1,
    stream_id: "stream-1",
    turn_id: turnId,
    text,
    transient: true,
  }
}

function saved(text: string): TranscriptMessage {
  return { id: "message-1", role: "assistant", status: "complete", text }
}

test("matching saved text completes a streamed preview without printing it twice", () => {
  const output: string[] = []
  const preview = new AssistantPreview("turn-1", (text) => output.push(text))

  expect(preview.consume(delta("another-turn", "ignored"))).toBe(false)
  expect(preview.consume(delta("turn-1", "Hello "))).toBe(true)
  expect(preview.consume(delta("turn-1", "world"))).toBe(true)
  preview.finish([saved("Hello world")], "idle")

  expect(output.join("")).toBe("PCBDraft: Hello world\n\n")
})

test("a saved response that differs from the preview is rendered as authoritative", () => {
  const output: string[] = []
  const preview = new AssistantPreview("turn-1", (text) => output.push(text))

  preview.consume(delta("turn-1", "Partial"))
  preview.finish([saved("Complete answer")], "idle")
  preview.finish([saved("must not render again")], "idle")

  expect(output.join("")).toBe(
    "PCBDraft: Partial\n\nPCBDraft (saved response): Complete answer\n\n",
  )
})

test("a turn without preview still prints its saved assistant messages", () => {
  const output: string[] = []
  const preview = new AssistantPreview("turn-1", (text) => output.push(text))

  preview.finish([saved("Saved only")], "idle")

  expect(output.join("")).toBe("PCBDraft: Saved only\n\n")
})
