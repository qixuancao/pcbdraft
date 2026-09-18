import { SLASH_COMMANDS } from "./commands.ts"

/**
 * Headless terminal view/state helpers.
 *
 * `layoutTui` returns fixed rows with semantic kinds and a cursor coordinate,
 * allowing an OpenTUI application to map the result to styled components. This
 * module does not write to a terminal or own raw-mode, screen, or ANSI lifecycle.
 */

export type TuiMessage = {
  role: "user" | "assistant" | "system"
  text: string
}

export type TuiProject = {
  name: string
  status?: string
}

export type TuiStatus = {
  text: string
  tone?: "idle" | "working" | "success" | "error"
}

export type InputBuffer = {
  value: string
  /** Cursor position in Unicode code points, not UTF-16 code units. */
  cursor: number
}

export type InputAction =
  | { type: "insert"; text: string }
  | { type: "backspace" }
  | { type: "delete" }
  | { type: "move"; delta: number }
  | { type: "home" }
  | { type: "end" }
  | { type: "set"; value: string; cursor?: number }
  | { type: "complete-command" }

export type TuiCommand = {
  name: string
  usage?: string
  description: string
}

export type TuiState = {
  title?: string
  project?: TuiProject | null
  messages: readonly TuiMessage[]
  status?: TuiStatus
  input: InputBuffer
  commands?: readonly TuiCommand[]
  selectedCommand?: number
  footer?: string
  /** Number of wrapped transcript rows to scroll back from the newest row. */
  scrollOffset?: number
}

export type TerminalSize = {
  columns: number
  rows: number
}

export type TuiLineKind =
  | "header"
  | "separator"
  | "chat-user"
  | "chat-assistant"
  | "chat-system"
  | "empty"
  | "status-idle"
  | "status-working"
  | "status-success"
  | "status-error"
  | "candidate"
  | "candidate-selected"
  | "input-border"
  | "input"
  | "footer"

export type TuiScreenLine = {
  text: string
  kind: TuiLineKind
}

export type TuiLayout = {
  columns: number
  rows: number
  compact: boolean
  lines: readonly TuiScreenLine[]
  cursor: { row: number; column: number }
}

const COMMAND_DETAILS: Record<(typeof SLASH_COMMANDS)[number], Omit<TuiCommand, "name">> = {
  new: { usage: "<name>", description: "Create and open a PCB project" },
  resume: { usage: "[text]", description: "Find projects to resume" },
  open: { usage: "<number|id>", description: "Open a project and restore its chat" },
  stop: { description: "Cancel the active turn" },
  help: { description: "Show terminal help" },
  quit: { description: "Exit PCBDraft" },
  exit: { description: "Exit PCBDraft" },
}

export const DEFAULT_TUI_COMMANDS: readonly TuiCommand[] = SLASH_COMMANDS.map((name) => ({
  name,
  ...COMMAND_DETAILS[name],
}))

const CONTROL_CHARACTERS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/gu
const MARK = /\p{Mark}/u

export function createInputBuffer(value = "", cursor?: number): InputBuffer {
  const normalized = normalizeInput(value)
  const length = codePoints(normalized).length
  return { value: normalized, cursor: clampInteger(cursor ?? length, 0, length) }
}

export function commandCandidates(
  input: string | InputBuffer,
  commands: readonly TuiCommand[] = DEFAULT_TUI_COMMANDS,
): TuiCommand[] {
  const value = typeof input === "string" ? normalizeInput(input) : normalizeInput(input.value)
  if (!value.startsWith("/") || /\s/u.test(value)) return []
  const prefix = value.slice(1).toLocaleLowerCase()
  return commands.filter((command) => command.name.toLocaleLowerCase().startsWith(prefix))
}

export function updateInputBuffer(
  input: InputBuffer,
  action: InputAction,
  commands: readonly TuiCommand[] = DEFAULT_TUI_COMMANDS,
): InputBuffer {
  const current = createInputBuffer(input.value, input.cursor)
  const characters = codePoints(current.value)

  if (action.type === "set") return createInputBuffer(action.value, action.cursor)
  if (action.type === "home") return { ...current, cursor: 0 }
  if (action.type === "end") return { ...current, cursor: characters.length }
  if (action.type === "move") {
    return { ...current, cursor: clampInteger(current.cursor + action.delta, 0, characters.length) }
  }
  if (action.type === "insert") {
    const inserted = codePoints(normalizeInput(action.text))
    characters.splice(current.cursor, 0, ...inserted)
    return { value: characters.join(""), cursor: current.cursor + inserted.length }
  }
  if (action.type === "backspace") {
    if (current.cursor === 0) return current
    characters.splice(current.cursor - 1, 1)
    return { value: characters.join(""), cursor: current.cursor - 1 }
  }
  if (action.type === "delete") {
    if (current.cursor === characters.length) return current
    characters.splice(current.cursor, 1)
    return { value: characters.join(""), cursor: current.cursor }
  }

  const matches = commandCandidates(current, commands)
  if (matches.length !== 1) return current
  return createInputBuffer(`/${matches[0]!.name} `)
}

export function layoutTui(state: TuiState, size: TerminalSize): TuiLayout {
  const columns = clampInteger(size.columns, 1, 1_000)
  const rows = clampInteger(size.rows, 1, 1_000)
  const compact = columns < 52 || rows < 10
  const commands = state.commands ?? DEFAULT_TUI_COMMANDS
  const matches = commandCandidates(state.input, commands)
  const showHeader = rows >= 4
  const showStatus = rows >= 3
  const showFooter = rows >= 2
  const framedInput = !compact && rows >= 8
  const showSeparator = !compact && rows >= 9 && showHeader
  const fixedRows = 1
    + Number(showHeader)
    + Number(showStatus)
    + Number(showFooter)
    + (framedInput ? 2 : 0)
    + Number(showSeparator)
  const flexibleRows = Math.max(0, rows - fixedRows)
  const desiredCandidates = Math.min(matches.length, compact ? 2 : 4)
  const candidateRows = Math.min(desiredCandidates, Math.max(0, flexibleRows - 1))
  const chatRows = flexibleRows - candidateRows
  const lines: TuiScreenLine[] = []

  if (showHeader) lines.push({ text: headerLine(state, columns, compact), kind: "header" })
  if (showSeparator) lines.push({ text: "─".repeat(columns), kind: "separator" })
  lines.push(...chatLines(state, columns, chatRows))
  if (showStatus) lines.push(statusLine(state, columns))
  lines.push(...candidateLines(matches, state.selectedCommand, columns, compact, candidateRows))

  let cursor: TuiLayout["cursor"]
  if (framedInput) {
    lines.push({ text: inputBorder("Message", columns, "top"), kind: "input-border" })
    const input = inputLine(state.input, columns, true)
    lines.push({ text: input.text, kind: "input" })
    cursor = { row: lines.length, column: input.cursorColumn }
    lines.push({ text: inputBorder("", columns, "bottom"), kind: "input-border" })
  } else {
    const input = inputLine(state.input, columns, false)
    lines.push({ text: input.text, kind: "input" })
    cursor = { row: lines.length, column: input.cursorColumn }
  }

  if (showFooter) lines.push({ text: footerLine(state, compact), kind: "footer" })
  while (lines.length < rows) lines.splice(Math.max(0, lines.length - Number(showFooter)), 0, { text: "", kind: "empty" })
  if (lines.length > rows) lines.length = rows

  return {
    columns,
    rows,
    compact,
    lines: lines.map((line) => ({ ...line, text: fitLine(line.text, columns) })),
    cursor: {
      row: clampInteger(cursor.row, 1, rows),
      column: clampInteger(cursor.column, 1, columns),
    },
  }
}

function headerLine(state: TuiState, columns: number, compact: boolean): string {
  const title = cleanInline(state.title ?? "PCBDraft")
  const project = state.project
    ? [cleanInline(state.project.name), cleanInline(state.project.status ?? "")].filter(Boolean).join(" · ")
    : "No project"
  if (compact) return clipText(`${title} · ${project}`, columns)

  const left = `${title} Terminal`
  const right = project
  const gap = columns - displayWidth(left) - displayWidth(right)
  if (gap >= 3) return `${left}${" ".repeat(gap)}${right}`
  return clipText(`${left} · ${right}`, columns)
}

function chatLines(state: TuiState, columns: number, height: number): TuiScreenLine[] {
  if (height <= 0) return []
  const width = Math.max(1, columns - (columns >= 52 ? 2 : 0))
  const indent = columns >= 52 ? " " : ""
  const all: TuiScreenLine[] = []
  for (const message of state.messages) {
    const label = message.role === "user" ? "You" : message.role === "assistant" ? "PCBDraft" : "System"
    const kind: TuiLineKind = message.role === "user"
      ? "chat-user"
      : message.role === "assistant"
        ? "chat-assistant"
        : "chat-system"
    for (const text of wrapText(`${label} › ${cleanMultiline(message.text)}`, width)) {
      all.push({ text: `${indent}${text}`, kind })
    }
    all.push({ text: "", kind: "empty" })
  }
  if (all.at(-1)?.kind === "empty") all.pop()

  const scrollOffset = clampInteger(state.scrollOffset ?? 0, 0, all.length)
  const end = Math.max(0, all.length - scrollOffset)
  const visible = all.slice(Math.max(0, end - height), end)
  return [
    ...Array.from({ length: height - visible.length }, (): TuiScreenLine => ({ text: "", kind: "empty" })),
    ...visible,
  ]
}

function statusLine(state: TuiState, columns: number): TuiScreenLine {
  const status = state.status ?? { text: "Ready", tone: "idle" as const }
  const tone = status.tone ?? "idle"
  const marker = tone === "working" ? "◐" : tone === "success" ? "✓" : tone === "error" ? "!" : "●"
  return {
    text: clipText(` ${marker} ${cleanInline(status.text)}`, columns),
    kind: `status-${tone}`,
  }
}

function candidateLines(
  matches: readonly TuiCommand[],
  selected: number | undefined,
  columns: number,
  compact: boolean,
  height: number,
): TuiScreenLine[] {
  if (height <= 0) return []
  const selectedIndex = clampInteger(selected ?? 0, 0, Math.max(0, matches.length - 1))
  return matches.slice(0, height).map((command, index) => {
    const marker = index === selectedIndex ? "›" : " "
    const invocation = `/${command.name}${command.usage ? ` ${command.usage}` : ""}`
    const text = compact
      ? ` ${marker} ${invocation}`
      : ` ${marker} ${invocation.padEnd(22)} ${cleanInline(command.description)}`
    return {
      text: clipText(text, columns),
      kind: index === selectedIndex ? "candidate-selected" : "candidate",
    }
  })
}

function inputBorder(label: string, columns: number, edge: "top" | "bottom"): string {
  if (columns === 1) return edge === "top" ? "┌" : "└"
  const left = edge === "top" ? "┌" : "└"
  const right = edge === "top" ? "┐" : "┘"
  const visibleLabel = label && columns >= 12 ? `─ ${label} ` : ""
  const middleWidth = Math.max(0, columns - 2)
  const middle = clipText(visibleLabel, middleWidth)
  return `${left}${middle}${"─".repeat(Math.max(0, middleWidth - displayWidth(middle)))}${right}`
}

function inputLine(input: InputBuffer, columns: number, framed: boolean): { text: string; cursorColumn: number } {
  const normalized = createInputBuffer(input.value, input.cursor)
  const prefix = framed ? "│ › " : "› "
  const suffix = framed ? "│" : ""
  const available = Math.max(1, columns - displayWidth(prefix) - displayWidth(suffix))
  const viewport = inputViewport(normalized, available)
  const text = `${prefix}${padToWidth(viewport.text, available)}${suffix}`
  return {
    text: clipText(text, columns, ""),
    cursorColumn: Math.min(columns, displayWidth(prefix) + viewport.cursorOffset + 1),
  }
}

function inputViewport(input: InputBuffer, width: number): { text: string; cursorOffset: number } {
  const characters = codePoints(input.value)
  const before = characters.slice(0, input.cursor).join("")
  const after = characters.slice(input.cursor).join("")
  const beforeLimit = Math.max(0, width - 1)
  let visibleBefore = takeEnd(before, beforeLimit)
  if (visibleBefore !== before && width >= 2) visibleBefore = `…${takeEnd(before, beforeLimit - 1)}`
  const cursorOffset = displayWidth(visibleBefore)
  const afterWidth = Math.max(0, width - cursorOffset)
  let visibleAfter = takeStart(after, afterWidth)
  if (visibleAfter !== after && afterWidth >= 1) {
    visibleAfter = afterWidth === 1 ? "…" : `${takeStart(after, afterWidth - 1)}…`
  }
  return { text: `${visibleBefore}${visibleAfter}`, cursorOffset }
}

function footerLine(state: TuiState, compact: boolean): string {
  if (state.footer) return cleanInline(state.footer)
  return compact ? " Tab complete · Enter send" : " Tab complete · Enter send · ↑↓ history · Ctrl+C quit"
}

function normalizeInput(value: string): string {
  return value.replaceAll("\r\n", " ").replaceAll("\r", " ").replaceAll("\n", " ").replaceAll("\t", "  ").replace(CONTROL_CHARACTERS, "")
}

function cleanInline(value: string): string {
  return normalizeInput(String(value))
}

function cleanMultiline(value: string): string {
  return String(value).replaceAll("\r\n", "\n").replaceAll("\r", "\n").replaceAll("\t", "  ").replace(CONTROL_CHARACTERS, "")
}

function codePoints(value: string): string[] {
  return Array.from(value)
}

function wrapText(value: string, width: number): string[] {
  const lines: string[] = []
  let line = ""
  let lineWidth = 0
  for (const character of codePoints(value)) {
    if (character === "\n") {
      lines.push(line)
      line = ""
      lineWidth = 0
      continue
    }
    const characterWidth = codePointWidth(character)
    if (line && lineWidth + characterWidth > width) {
      lines.push(line.trimEnd())
      line = character === " " ? "" : character
      lineWidth = character === " " ? 0 : characterWidth
      continue
    }
    if (!line && characterWidth > width) continue
    line += character
    lineWidth += characterWidth
  }
  lines.push(line.trimEnd())
  return lines.length ? lines : [""]
}

function fitLine(value: string, width: number): string {
  return padToWidth(clipText(value, width), width)
}

function clipText(value: string, width: number, ellipsis = "…"): string {
  if (width <= 0) return ""
  if (displayWidth(value) <= width) return value
  if (!ellipsis || displayWidth(ellipsis) >= width) return takeStart(value, width)
  return `${takeStart(value, width - displayWidth(ellipsis))}${ellipsis}`
}

function padToWidth(value: string, width: number): string {
  return `${value}${" ".repeat(Math.max(0, width - displayWidth(value)))}`
}

function takeStart(value: string, width: number): string {
  let result = ""
  let used = 0
  for (const character of codePoints(value)) {
    const next = codePointWidth(character)
    if (used + next > width) break
    result += character
    used += next
  }
  return result
}

function takeEnd(value: string, width: number): string {
  const kept: string[] = []
  let used = 0
  for (const character of codePoints(value).reverse()) {
    const next = codePointWidth(character)
    if (used + next > width) break
    kept.push(character)
    used += next
  }
  return kept.reverse().join("")
}

function displayWidth(value: string): number {
  return codePoints(value).reduce((width, character) => width + codePointWidth(character), 0)
}

function codePointWidth(character: string): number {
  if (MARK.test(character)) return 0
  const point = character.codePointAt(0) ?? 0
  if (point === 0) return 0
  if (
    point >= 0x1100
    && (point <= 0x115f
      || point === 0x2329
      || point === 0x232a
      || (point >= 0x2e80 && point <= 0xa4cf && point !== 0x303f)
      || (point >= 0xac00 && point <= 0xd7a3)
      || (point >= 0xf900 && point <= 0xfaff)
      || (point >= 0xfe10 && point <= 0xfe19)
      || (point >= 0xfe30 && point <= 0xfe6f)
      || (point >= 0xff00 && point <= 0xff60)
      || (point >= 0xffe0 && point <= 0xffe6)
      || (point >= 0x1f300 && point <= 0x1faff)
      || (point >= 0x20000 && point <= 0x3fffd))
  ) return 2
  return 1
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.min(maximum, Math.max(minimum, Math.trunc(value)))
}
