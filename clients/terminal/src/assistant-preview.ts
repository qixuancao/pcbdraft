import type { GuiEvent, TranscriptMessage } from "./bridge.ts"

type Writer = (text: string) => void

/** Render one turn's transient deltas while keeping saved session text authoritative. */
export class AssistantPreview {
  #closed = false
  #started = false
  #text = ""

  constructor(readonly turnId: string, private readonly write: Writer) {}

  get started(): boolean {
    return this.#started
  }

  consume(event: GuiEvent): boolean {
    if (this.#closed
      || event.kind !== "assistant.delta"
      || event.turn_id !== this.turnId
      || event.transient !== true
      || !event.text) return false
    if (!this.#started) {
      this.write("PCBDraft: ")
      this.#started = true
    }
    this.#text += event.text
    this.write(event.text)
    return true
  }

  finish(messages: TranscriptMessage[], status: string): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#started) this.write("\n\n")

    const matchingPreview = this.#started
      ? messages.findIndex((message) => message.text === this.#text)
      : -1
    messages.forEach((message, index) => {
      if (index === matchingPreview) return
      const label = matchingPreview < 0 && this.#started
        ? "PCBDraft (saved response)"
        : "PCBDraft"
      this.write(`${label}: ${message.text}\n\n`)
    })

    if (messages.length === 0 && status === "idle") {
      this.write(this.#started
        ? "The preview was not saved as an assistant message.\n"
        : "Job finished without a new assistant message.\n")
    }
  }
}
