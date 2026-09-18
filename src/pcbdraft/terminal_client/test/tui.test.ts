import { expect, test } from "bun:test"
import {
  commandCandidates,
  createInputBuffer,
  layoutTui,
  updateInputBuffer,
  type TuiState,
} from "../src/tui.ts"

function state(overrides: Partial<TuiState> = {}): TuiState {
  return {
    project: { name: "test1", status: "generated" },
    messages: [
      { role: "user", text: "Place a USB-C connector" },
      { role: "assistant", text: "I placed J1 and routed the power pins." },
    ],
    status: { text: "Ready", tone: "idle" },
    input: createInputBuffer(""),
    ...overrides,
  }
}

test("lays out a complete wide terminal frame with command candidates", () => {
  const layout = layoutTui(state({ input: createInputBuffer("/res") }), { columns: 80, rows: 16 })

  expect(layout.compact).toBeFalse()
  expect(layout.lines).toHaveLength(16)
  expect(layout.lines[0]!.text).toContain("PCBDraft Terminal")
  expect(layout.lines[0]!.text).toContain("test1 · generated")
  expect(layout.lines.some((line) => line.text.includes("You › Place a USB-C connector"))).toBeTrue()
  expect(layout.lines.some((line) => line.text.includes("/resume [text]"))).toBeTrue()
  expect(layout.lines.some((line) => line.text.includes("Find projects to resume"))).toBeTrue()
  expect(layout.lines.some((line) => line.text.startsWith("┌─ Message"))).toBeTrue()
  expect(layout.lines.every((line) => line.text.length === 80)).toBeTrue()
  expect(layout.cursor.row).toBeGreaterThan(1)
  expect(layout.cursor.column).toBeGreaterThan(1)
})

test("keeps the headless view free of terminal control sequences", () => {
  const layout = layoutTui(state({
    project: { name: "bad\u001b[2Jname", status: "ready" },
    messages: [{ role: "system", text: "safe\u001b[31m text" }],
  }), { columns: 64, rows: 12 })
  const screen = layout.lines.map((line) => line.text).join("\n")

  expect(screen).toContain("bad[2Jname")
  expect(screen).toContain("safe[31m text")
  expect(screen).not.toContain("\u001b")
})

test("degrades to compact rows on narrow terminals", () => {
  const layout = layoutTui(state({ input: createInputBuffer("/") }), { columns: 28, rows: 8 })

  expect(layout.compact).toBeTrue()
  expect(layout.lines).toHaveLength(8)
  expect(layout.lines[0]!.text.trimEnd()).toBe("PCBDraft · test1 · generated")
  expect(layout.lines.some((line) => line.text.includes("Find projects"))).toBeFalse()
  expect(layout.lines.some((line) => line.text.startsWith("┌"))).toBeFalse()
  expect(layout.lines.every((line) => line.text.length === 28)).toBeTrue()
  expect(layout.cursor.column).toBeLessThanOrEqual(28)
})

test("keeps the newest transcript rows when the chat viewport overflows", () => {
  const layout = layoutTui(state({
    messages: Array.from({ length: 12 }, (_, index) => ({
      role: "assistant" as const,
      text: `message-${index}`,
    })),
  }), { columns: 60, rows: 10 })
  const screen = layout.lines.map((line) => line.text).join("\n")

  expect(screen).toContain("message-11")
  expect(screen).not.toContain("message-0")
})

test("finds case-insensitive command candidates without matching arguments", () => {
  expect(commandCandidates("/RES").map((command) => command.name)).toEqual(["resume"])
  expect(commandCandidates("/").map((command) => command.name)).toEqual([
    "new", "resume", "open", "stop", "help", "quit", "exit",
  ])
  expect(commandCandidates("/resume board")).toEqual([])
  expect(commandCandidates("hello")).toEqual([])
})

test("edits Unicode input by code point and completes only unique commands", () => {
  let input = createInputBuffer("电路")
  input = updateInputBuffer(input, { type: "move", delta: -1 })
  input = updateInputBuffer(input, { type: "insert", text: "板" })
  expect(input).toEqual({ value: "电板路", cursor: 2 })
  input = updateInputBuffer(input, { type: "backspace" })
  expect(input).toEqual({ value: "电路", cursor: 1 })
  input = updateInputBuffer(input, { type: "delete" })
  expect(input).toEqual({ value: "电", cursor: 1 })

  expect(updateInputBuffer(createInputBuffer("/res"), { type: "complete-command" })).toEqual({
    value: "/resume ",
    cursor: 8,
  })
  expect(updateInputBuffer(createInputBuffer("/"), { type: "complete-command" })).toEqual({
    value: "/",
    cursor: 1,
  })
})

test("keeps the cursor visible when a long input scrolls horizontally", () => {
  const input = createInputBuffer("0123456789abcdefghijklmnopqrstuvwxyz")
  const layout = layoutTui(state({ input }), { columns: 20, rows: 5 })
  const inputLine = layout.lines[layout.cursor.row - 1]!.text

  expect(inputLine).toContain("…")
  expect(inputLine).toContain("xyz")
  expect(layout.cursor.column).toBeLessThanOrEqual(20)
})
