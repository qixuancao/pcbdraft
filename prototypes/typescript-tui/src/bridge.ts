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

type Bootstrap = {
  csrf_token: string
  projects: Project[]
}

export type ProjectSession = {
  project_id: string
  status: string
  messages: TranscriptMessage[]
  active_turn: { job_id: string; turn_id: string; status: string } | null
}

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

  async session(projectId: string): Promise<ProjectSession> {
    return this.#get<ProjectSession>(`/api/projects/${encodeURIComponent(projectId)}/session`)
  }

  async sendMessage(projectId: string, text: string): Promise<{ job_id: string; turn_id: string; status: string }> {
    return this.#post(`/api/projects/${encodeURIComponent(projectId)}/messages`, { text })
  }

  async stop(projectId: string): Promise<{ job_id: string | null; status: string }> {
    return this.#post(`/api/projects/${encodeURIComponent(projectId)}/stop`, {})
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
