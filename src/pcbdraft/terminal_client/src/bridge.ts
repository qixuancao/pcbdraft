export type Project = {
  id: string
  name: string
  status: string
  updated_at?: string
  design_revision?: number
}

export type TranscriptMessage = {
  role: "user" | "assistant"
  text: string
  id: string
  status: string
}

export type ProjectActiveTurn = {
  job_id: string
  turn_id: string | null
  status: string
  started_at: string | null
}

type Bootstrap = {
  csrf_token: string
  projects: Project[]
}

export type ProjectSession = {
  schema: "pcbdraft-gui-session"
  version: 2
  project_id: string
  status: string
  messages: TranscriptMessage[]
  active_turn: ProjectActiveTurn | null
}

export type MonitorableProjectSession = Omit<ProjectSession, "active_turn"> & {
  active_turn: (ProjectActiveTurn & { turn_id: string }) | null
}

export type GuiEvent = {
  kind: string
  message: string
  level: string
  created_at: string
  source: string
  sequence: number
  stream_id: string
  state?: string
  turn_id?: string
  text?: string
  transient?: boolean
}

export type ProjectSnapshot = {
  schema: "pcbdraft-gui-snapshot"
  version: 2
  session: ProjectSession
  stream: { stream_id: string; last_sequence: number; oldest_sequence: number | null }
}

export type GuiEventHandler = (event: GuiEvent) => boolean | void | Promise<boolean | void>

export class GuiClient {
  #csrfToken: string | null = null

  constructor(readonly baseUrl = process.env.PCBDRAFT_GUI_URL ?? "http://127.0.0.1:9130") {}

  async bootstrap(): Promise<Bootstrap> {
    const value = await this.#get<Bootstrap>("/api/bootstrap")
    this.#csrfToken = value.csrf_token
    return value
  }

  async projects(query = ""): Promise<Project[]> {
    const value = await this.#get<{ projects: Project[] }>("/api/projects")
    const needle = query.toLocaleLowerCase().trim()
    return value.projects.filter((project) => !needle || project.id.toLocaleLowerCase().includes(needle) || project.name.toLocaleLowerCase().includes(needle))
  }

  async createProject(name: string): Promise<Project> {
    const value = await this.#post<{ project: Project }>("/api/projects", { name })
    return value.project
  }

  async session(projectId: string): Promise<MonitorableProjectSession> {
    const session = decodeProjectSession(
      await this.#get<unknown>(`/api/projects/${encodeURIComponent(projectId)}/session`),
    )
    // A v2 active job may have no canonical turn ID. The terminal cannot
    // correlate assistant deltas in that case, so expose it as unmonitorable.
    const active = session.active_turn
    if (!active || active.turn_id === null) return { ...session, active_turn: null }
    return { ...session, active_turn: { ...active, turn_id: active.turn_id } }
  }

  async snapshot(projectId: string): Promise<ProjectSnapshot> {
    return decodeProjectSnapshot(
      await this.#get<unknown>(`/api/projects/${encodeURIComponent(projectId)}/snapshot`),
    )
  }

  async sendMessage(projectId: string, text: string): Promise<{ job_id: string; turn_id: string; status: string }> {
    return this.#post(`/api/projects/${encodeURIComponent(projectId)}/messages`, { text })
  }

  async stop(projectId: string): Promise<{ job_id: string | null; status: string }> {
    return this.#post(`/api/projects/${encodeURIComponent(projectId)}/stop`, {})
  }

  /**
   * Consume the canonical GUI lifecycle stream. Returning false from the
   * handler closes this subscription. These events describe lifecycle state;
   * assistant text remains authoritative in session().
   */
  async subscribe(projectId: string, handler: GuiEventHandler): Promise<number>
  async subscribe(
    projectId: string,
    after: number | undefined,
    handler: GuiEventHandler,
    signal?: AbortSignal,
  ): Promise<number>
  async subscribe(
    projectId: string,
    afterOrHandler: number | undefined | GuiEventHandler,
    handlerMaybe?: GuiEventHandler,
    signal?: AbortSignal,
  ): Promise<number> {
    const cursor = typeof afterOrHandler === "function" ? 0 : (afterOrHandler ?? 0)
    const handler = typeof afterOrHandler === "function" ? afterOrHandler : handlerMaybe
    if (!handler) throw new Error("GUI event subscription requires a handler")
    if (!Number.isSafeInteger(cursor) || cursor < 0) throw new Error("Event cursor must be a non-negative integer")
    const url = new URL(`/api/projects/${encodeURIComponent(projectId)}/events`, this.baseUrl)
    url.searchParams.set("after", String(cursor))
    const response = await fetch(url, { headers: { Accept: "text/event-stream" }, signal })
    if (!response.ok) return this.#decode<never>(response)
    if (!response.body) throw new Error("GUI event stream has no response body")

    let latest = cursor
    for await (const record of sseRecords(response.body)) {
      if (record.event !== "update" || !record.data) continue
      const value: unknown = JSON.parse(record.data)
      if (!isGuiEvent(value)) throw new Error("GUI event stream returned an invalid event")
      latest = value.sequence
      if (await handler(value) === false) break
    }
    return latest
  }

  async #get<T>(path: string): Promise<T> {
    const response = await fetch(new URL(path, this.baseUrl))
    return this.#decode<T>(response)
  }

  async #post<T>(path: string, body: object): Promise<T> {
    if (!this.#csrfToken) await this.bootstrap()
    const payload = JSON.stringify(body)
    const response = await fetch(new URL(path, this.baseUrl), {
      method: "POST",
      headers: {
        Origin: this.baseUrl,
        "Content-Type": "application/json",
        "X-PCBDraft-CSRF": this.#csrfToken ?? "",
      },
      body: payload,
    })
    return this.#decode<T>(response)
  }

  async #decode<T>(response: Response): Promise<T> {
    const body: unknown = await response.json()
    if (!response.ok) {
      const message = typeof body === "object" && body && "error" in body
        ? String((body as { error?: { message?: unknown } }).error?.message ?? "GUI request failed")
        : "GUI request failed"
      throw new Error(message)
    }
    return body as T
  }
}

type SseRecord = { event: string; data: string }

async function* sseRecords(body: ReadableStream<Uint8Array>): AsyncGenerator<SseRecord> {
  const decoder = new TextDecoder()
  const reader = body.getReader()
  let pending = ""
  try {
    while (true) {
      const { done, value } = await reader.read()
      pending += decoder.decode(value, { stream: !done })
      let boundary = /\r?\n\r?\n/.exec(pending)
      while (boundary?.index !== undefined) {
        const block = pending.slice(0, boundary.index)
        pending = pending.slice(boundary.index + boundary[0].length)
        const record = parseSseRecord(block)
        if (record) yield record
        boundary = /\r?\n\r?\n/.exec(pending)
      }
      if (done) break
    }
    const record = parseSseRecord(pending)
    if (record) yield record
  } finally {
    try {
      await reader.cancel()
    } finally {
      reader.releaseLock()
    }
  }
}

function parseSseRecord(block: string): SseRecord | null {
  let event = "message"
  const data: string[] = []
  for (const line of block.replaceAll("\r\n", "\n").split("\n")) {
    if (!line || line.startsWith(":")) continue
    const separator = line.indexOf(":")
    const field = separator < 0 ? line : line.slice(0, separator)
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "")
    if (field === "event") event = value
    if (field === "data") data.push(value)
  }
  return data.length ? { event, data: data.join("\n") } : null
}

function isGuiEvent(value: unknown): value is GuiEvent {
  if (!value || typeof value !== "object") return false
  const event = value as Partial<GuiEvent>
  const baseIsValid = typeof event.kind === "string"
    && typeof event.message === "string"
    && typeof event.level === "string"
    && typeof event.created_at === "string"
    && typeof event.source === "string"
    && Number.isSafeInteger(event.sequence)
    && (event.sequence ?? 0) > 0
    && typeof event.stream_id === "string"
    && (event.state === undefined || typeof event.state === "string")
    && (event.turn_id === undefined || typeof event.turn_id === "string")
    && (event.text === undefined || typeof event.text === "string")
    && (event.transient === undefined || typeof event.transient === "boolean")
  if (!baseIsValid) return false
  if (event.kind !== "assistant.delta") return true
  return event.source === "agent"
    && event.transient === true
    && typeof event.turn_id === "string"
    && event.turn_id.length > 0
    && event.turn_id.length <= 128
    && typeof event.text === "string"
    && event.text.length > 0
    && event.text.length <= 4_096
}

function decodeProjectSession(value: unknown): ProjectSession {
  if (!isRecord(value) || value.schema !== "pcbdraft-gui-session" || value.version !== 2) {
    throw new Error("GUI session returned an unsupported schema/version")
  }
  if (
    typeof value.project_id !== "string"
    || typeof value.status !== "string"
    || !Array.isArray(value.messages)
    || !value.messages.every(isTranscriptMessage)
    || !isProjectActiveTurn(value.active_turn)
  ) throw new Error("GUI session returned an invalid v2 payload")
  return value as ProjectSession
}

function decodeProjectSnapshot(value: unknown): ProjectSnapshot {
  if (!isRecord(value) || value.schema !== "pcbdraft-gui-snapshot" || value.version !== 2) {
    throw new Error("GUI snapshot returned an unsupported schema/version")
  }
  const session = decodeProjectSession(value.session)
  const stream = value.stream
  if (
    !isRecord(stream)
    || typeof stream.stream_id !== "string"
    || !isNonNegativeInteger(stream.last_sequence)
    || (stream.oldest_sequence !== null && !isNonNegativeInteger(stream.oldest_sequence))
  ) throw new Error("GUI snapshot returned an invalid v2 payload")
  return { ...value, session } as ProjectSnapshot
}

function isTranscriptMessage(value: unknown): value is TranscriptMessage {
  if (!isRecord(value)) return false
  return (value.role === "user" || value.role === "assistant")
    && typeof value.text === "string"
    && typeof value.id === "string"
    && typeof value.status === "string"
}

function isProjectActiveTurn(value: unknown): value is ProjectActiveTurn | null {
  if (value === null) return true
  if (!isRecord(value)) return false
  return typeof value.job_id === "string"
    && (value.turn_id === null || typeof value.turn_id === "string")
    && typeof value.status === "string"
    && (value.started_at === null || typeof value.started_at === "string")
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0
}
