import {
  BoxRenderable,
  createCliRenderer,
  InputRenderable,
  InputRenderableEvents,
  ScrollBoxRenderable,
  TextRenderable,
  type CliRenderer,
  type KeyEvent,
  type Renderable,
} from "@opentui/core"
import type { TranscriptMessage } from "./bridge.ts"
import {
  commandCandidates,
  createInputBuffer,
  type TuiCommand,
  type TuiStatus,
} from "./tui.ts"

export type OpenTuiState = {
  title?: string
  project?: { name: string; status?: string } | null
  messages: readonly TranscriptMessage[]
  preview?: string
  status?: TuiStatus
  input?: string
  commands?: readonly TuiCommand[]
  footer?: string
  notices?: readonly string[]
}

export type OpenTuiHandlers = {
  submit: (text: string) => void | Promise<void>
  stop: () => void | Promise<void>
  quit: () => void | Promise<void>
}

const COLORS = {
  title: "#8be9fd",
  muted: "#8b949e",
  user: "#f1fa8c",
  assistant: "#f8f8f2",
  system: "#bd93f9",
  working: "#ffb86c",
  success: "#50fa7b",
  error: "#ff5555",
}

/**
 * OpenCode-style terminal shell for PCBDraft.
 *
 * The GUI client and the project/session controller remain outside this
 * class. This object owns only the renderer and terminal presentation, so
 * asynchronous SSE updates and keyboard input always converge through one
 * render tree instead of competing stdout writes.
 */
export class OpenTuiApp {
  #renderer: CliRenderer | null = null
  #root: BoxRenderable | null = null
  #header: TextRenderable | null = null
  #chat: ScrollBoxRenderable | null = null
  #status: TextRenderable | null = null
  #suggestions: TextRenderable | null = null
  #composer: BoxRenderable | null = null
  #input: InputRenderable | null = null
  #footer: TextRenderable | null = null
  #previewNode: TextRenderable | null = null
  #transcriptSignature = ""
  #noticeSignature = ""
  #state: OpenTuiState = { messages: [] }
  #closed = false

  constructor(private readonly handlers: OpenTuiHandlers) {}

  get renderer(): CliRenderer | null {
    return this.#renderer
  }

  get inputValue(): string {
    return this.#input?.value ?? this.#state.input ?? ""
  }

  async start(state: OpenTuiState = this.#state): Promise<void> {
    if (this.#renderer) return
    this.#state = state
    this.#renderer = await createCliRenderer({
      screenMode: "alternate-screen",
      exitOnCtrlC: false,
      clearOnShutdown: true,
      targetFps: 30,
      maxFps: 60,
      useMouse: false,
    })
    this.#buildTree(this.#renderer)
    this.#installInputHandlers(this.#renderer)
    this.update(state)
    this.#input?.focus()
    this.#renderer.start()
  }

  update(state: OpenTuiState): void {
    this.#state = state
    if (!this.#renderer || !this.#root) return
    this.#syncText()
    this.#renderer.requestRender()
  }

  setInput(value: string): void {
    this.#state = { ...this.#state, input: value }
    if (this.#input) this.#input.value = value
    this.#syncSuggestions()
    this.#renderer?.requestRender()
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    const renderer = this.#renderer
    this.#renderer = null
    this.#root = null
    if (renderer && !renderer.isDestroyed) renderer.destroy()
  }

  #buildTree(renderer: CliRenderer): void {
    const root = new BoxRenderable(renderer, {
      flexDirection: "column",
      width: "100%",
      height: "100%",
      padding: 1,
      backgroundColor: "#0d1117",
    })
    const header = new TextRenderable(renderer, {
      width: "100%",
      height: 1,
      fg: COLORS.title,
      truncate: true,
    })
    const chat = new ScrollBoxRenderable(renderer, {
      flexGrow: 1,
      flexShrink: 1,
      width: "100%",
      border: true,
      borderColor: "#30363d",
      padding: 1,
      stickyScroll: true,
      stickyStart: "bottom",
      scrollY: true,
      scrollX: false,
    })
    const status = new TextRenderable(renderer, {
      width: "100%",
      height: 1,
      truncate: true,
      fg: COLORS.muted,
    })
    const suggestions = new TextRenderable(renderer, {
      width: "100%",
      height: 1,
      truncate: true,
      fg: COLORS.muted,
      visible: false,
    })
    const composer = new BoxRenderable(renderer, {
      width: "100%",
      height: 3,
      border: true,
      borderColor: "#58a6ff",
      title: " Message ",
      titleColor: "#58a6ff",
      paddingX: 1,
    })
    const input = new InputRenderable(renderer, {
      width: "100%",
      value: this.#state.input ?? "",
      placeholder: "Describe the PCB change…",
      textColor: COLORS.assistant,
      focusedTextColor: COLORS.assistant,
      backgroundColor: "#0d1117",
      focusedBackgroundColor: "#0d1117",
      placeholderColor: COLORS.muted,
    })
    const footer = new TextRenderable(renderer, {
      width: "100%",
      height: 1,
      truncate: true,
      fg: COLORS.muted,
    })

    composer.add(input)
    root.add(header)
    root.add(chat)
    root.add(status)
    root.add(suggestions)
    root.add(composer)
    root.add(footer)
    renderer.root.add(root)

    input.on(InputRenderableEvents.ENTER, () => {
      const value = input.value.trim()
      if (!value) return
      input.value = ""
      this.#state = { ...this.#state, input: "" }
      this.#syncSuggestions()
      void this.handlers.submit(value)
    })

    this.#root = root
    this.#header = header
    this.#chat = chat
    this.#status = status
    this.#suggestions = suggestions
    this.#composer = composer
    this.#input = input
    this.#footer = footer
  }

  #installInputHandlers(renderer: CliRenderer): void {
    // OpenTUI handles raw input and escape-sequence framing. Intercept only
    // the shortcuts that belong to the shell, before the composer consumes
    // them.
    renderer.prependInputHandler((sequence) => {
      if (sequence === "\t") {
        const input = this.#input
        if (!input) return true
        const matches = commandCandidates(createInputBuffer(input.value), this.#state.commands)
        if (matches.length === 1) {
          input.value = `/${matches[0]!.name} `
          this.#state = { ...this.#state, input: input.value }
          this.#syncSuggestions()
          renderer.requestRender()
        }
        return true
      }
      if (sequence === "\u0003") {
        void this.handlers.stop()
        return true
      }
      if (sequence === "\u0004") {
        void this.handlers.quit()
        return true
      }
      return false
    })
    renderer.keyInput.on("keypress", (key: KeyEvent) => {
      if (key.name === "escape") void this.handlers.stop()
    })
  }

  #syncText(): void {
    const state = this.#state
    if (this.#header) {
      const project = state.project
        ? `${state.project.name}${state.project.status ? ` · ${state.project.status}` : ""}`
        : "No project"
      this.#header.content = `  PCBDraft  ·  ${project}`
    }
    if (this.#chat) {
      const transcriptSignature = state.messages.map((message) => `${message.id}:${message.status}:${message.text}`).join("\u0000")
      const noticeSignature = (state.notices ?? []).join("\u0000")
      const staticChanged = transcriptSignature !== this.#transcriptSignature || noticeSignature !== this.#noticeSignature
      if (staticChanged) {
        for (const child of this.#chat.getChildren()) this.#chat.remove(child)
        for (const message of state.messages) this.#chat.add(this.#messageRenderable(message))
        for (const notice of state.notices ?? []) {
          this.#chat.add(new TextRenderable(this.#renderer!, {
            width: "100%",
            height: "auto",
            wrapMode: "word",
            fg: COLORS.system,
            content: `System › ${notice}\n`,
          }))
        }
        this.#previewNode = null
        this.#transcriptSignature = transcriptSignature
        this.#noticeSignature = noticeSignature
      }
      if (state.preview) {
        if (!this.#previewNode) {
          this.#previewNode = new TextRenderable(this.#renderer!, {
            width: "100%",
            height: "auto",
            wrapMode: "word",
            fg: COLORS.assistant,
          })
          this.#chat.add(this.#previewNode)
        }
        this.#previewNode.content = `PCBDraft › ${state.preview}`
      } else if (this.#previewNode) {
        this.#chat.remove(this.#previewNode)
        this.#previewNode = null
      }
      if (staticChanged || state.preview) this.#chat.scrollTo({ x: 0, y: this.#chat.scrollHeight })
    }
    if (this.#status) {
      const status = state.status ?? { text: "Ready", tone: "idle" as const }
      const marker = status.tone === "working" ? "◐" : status.tone === "success" ? "✓" : status.tone === "error" ? "!" : "●"
      this.#status.content = ` ${marker} ${status.text}`
      this.#status.fg = status.tone === "working" ? COLORS.working : status.tone === "success" ? COLORS.success : status.tone === "error" ? COLORS.error : COLORS.muted
    }
    this.#syncSuggestions()
    if (this.#footer) this.#footer.content = `  Enter send  ·  Tab complete  ·  Esc/Ctrl-C stop  ·  Ctrl-D quit${state.footer ? `  ·  ${state.footer}` : ""}`
    if (this.#input && this.#input.value !== (state.input ?? "")) this.#input.value = state.input ?? ""
  }

  #syncSuggestions(): void {
    if (!this.#suggestions || !this.#input) return
    const matches = commandCandidates(createInputBuffer(this.#input.value), this.#state.commands)
    if (matches.length === 0) {
      this.#suggestions.visible = false
      this.#suggestions.content = ""
      if (this.#composer) this.#composer.y = 0
      return
    }
    this.#suggestions.visible = true
    const preview = matches.slice(0, 4).map((command) => `/${command.name}${command.usage ? ` ${command.usage}` : ""}`).join("   ")
    this.#suggestions.content = `  ${preview}`
  }

  #messageRenderable(message: TranscriptMessage): Renderable {
    const role = message.role === "user" ? "You" : "PCBDraft"
    return new TextRenderable(this.#renderer!, {
      width: "100%",
      height: "auto",
      wrapMode: "word",
      fg: message.role === "user" ? COLORS.user : COLORS.assistant,
      content: `${role} › ${message.text}\n`,
    })
  }
}
