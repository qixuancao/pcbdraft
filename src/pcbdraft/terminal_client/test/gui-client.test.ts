import { afterEach, expect, test } from "bun:test"
import { GuiClient, type ProjectSession } from "../src/bridge.ts"

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

test("the TypeScript client bootstraps before it mutates a project", async () => {
  const requests: Array<{ url: string; init?: RequestInit }> = []
  globalThis.fetch = (async (input, init) => {
    const url = input instanceof Request ? input.url : input.toString()
    requests.push({ url, init })
    if (url.endsWith("/api/bootstrap")) {
      return Response.json({ csrf_token: "csrf", projects: [] })
    }
    return Response.json({ job_id: "job-1", turn_id: "turn-1", status: "queued" }, { status: 202 })
  }) as typeof fetch

  const client = new GuiClient("http://127.0.0.1:9130")
  await client.sendMessage("board-1", "make it smaller")

  expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
    "/api/bootstrap",
    "/api/projects/board-1/messages",
  ])
  expect(new Headers(requests[1]?.init?.headers).get("x-pcbdraft-csrf")).toBe("csrf")
  expect(new Headers(requests[1]?.init?.headers).get("origin")).toBe("http://127.0.0.1:9130")
})

test("createProject sends only the name and returns the public project summary", async () => {
  const requests: Array<{ url: string; init?: RequestInit }> = []
  globalThis.fetch = (async (input, init) => {
    const url = input instanceof Request ? input.url : input.toString()
    requests.push({ url, init })
    if (url.endsWith("/api/bootstrap")) {
      return Response.json({ csrf_token: "csrf", projects: [] })
    }
    return Response.json({
      project: {
        id: "sensor-board-ab12cd34",
        name: "Sensor board",
        status: "generated",
        updated_at: "2026-08-30T00:00:02Z",
        design_revision: 1,
      },
    }, { status: 201 })
  }) as typeof fetch

  const client = new GuiClient("http://127.0.0.1:9130")
  const project = await client.createProject("Sensor board")

  expect(project.id).toBe("sensor-board-ab12cd34")
  expect(new URL(requests[1]!.url).pathname).toBe("/api/projects")
  expect(requests[1]!.init?.method).toBe("POST")
  expect(requests[1]!.init?.body).toBe(JSON.stringify({ name: "Sensor board" }))
  expect(new Headers(requests[1]!.init?.headers).get("x-pcbdraft-csrf")).toBe("csrf")
})

function sessionPayload(overrides: Partial<ProjectSession> = {}): ProjectSession {
  return {
    schema: "pcbdraft-gui-session",
    version: 2,
    project_id: "board-1",
    status: "idle",
    messages: [],
    active_turn: null,
    ...overrides,
  }
}

test("session accepts the nullable v2 turn ID without exposing an unsafe monitor target", async () => {
  const nullableActiveTurn: ProjectSession["active_turn"] = {
    job_id: "job-1",
    turn_id: null,
    status: "running",
    started_at: "2026-09-18T00:00:00Z",
  }
  globalThis.fetch = (async (_input, _init) => Response.json(sessionPayload({
    status: "running",
    active_turn: nullableActiveTurn,
  }))) as typeof fetch

  const session = await new GuiClient("http://127.0.0.1:9130").session("board-1")

  expect(session.schema).toBe("pcbdraft-gui-session")
  expect(session.version).toBe(2)
  expect(session.status).toBe("running")
  expect(session.active_turn).toBeNull()
})

test("session rejects an unsupported or missing contract version", async () => {
  for (const version of [3, undefined]) {
    globalThis.fetch = (async (_input, _init) => Response.json({
      ...sessionPayload(),
      version,
    })) as typeof fetch

    const client = new GuiClient("http://127.0.0.1:9130")
    await expect(client.session("board-1")).rejects.toThrow(
      "GUI session returned an unsupported schema/version",
    )
  }
})

test("snapshot validates its v2 envelope, nested session, and stream cursor", async () => {
  globalThis.fetch = (async (_input, _init) => Response.json({
    schema: "pcbdraft-gui-snapshot",
    version: 2,
    session: sessionPayload(),
    stream: { stream_id: "stream-1", last_sequence: 7, oldest_sequence: null },
  })) as typeof fetch

  const snapshot = await new GuiClient("http://127.0.0.1:9130").snapshot("board-1")

  expect(snapshot.schema).toBe("pcbdraft-gui-snapshot")
  expect(snapshot.version).toBe(2)
  expect(snapshot.session.version).toBe(2)
  expect(snapshot.stream.last_sequence).toBe(7)
})

test("snapshot rejects a valid-looking stream wrapped in the wrong schema", async () => {
  globalThis.fetch = (async (_input, _init) => Response.json({
    schema: "pcbdraft-gui-snapshot-next",
    version: 2,
    session: sessionPayload(),
    stream: { stream_id: "stream-1", last_sequence: 7, oldest_sequence: null },
  })) as typeof fetch

  const client = new GuiClient("http://127.0.0.1:9130")
  await expect(client.snapshot("board-1")).rejects.toThrow(
    "GUI snapshot returned an unsupported schema/version",
  )
})

test("subscribe resumes the SSE stream and stops when the handler returns false", async () => {
  const requests: string[] = []
  globalThis.fetch = (async (input) => {
    const url = input instanceof Request ? input.url : input.toString()
    requests.push(url)
    return new Response([
      ": keepalive\r\n\r\n",
      "id: 8\r\nevent: update\r\ndata: {\"kind\":\"job.started\",\"message\":\"Job started\",\"level\":\"info\",\"created_at\":\"now\",\"source\":\"project\",\"sequence\":8,\"stream_id\":\"stream-1\"}\r\n\r\n",
      "id: 9\r\nevent: update\r\ndata: {\"kind\":\"job.complete\",\"message\":\"Job complete\",\"level\":\"info\",\"created_at\":\"later\",\"source\":\"project\",\"sequence\":9,\"stream_id\":\"stream-1\"}\r\n\r\n",
      "id: 10\r\nevent: update\r\ndata: {\"kind\":\"scene.committed\",\"message\":\"Scene\",\"level\":\"info\",\"created_at\":\"later\",\"source\":\"scene\",\"sequence\":10,\"stream_id\":\"stream-1\"}\r\n\r\n",
    ].join(""), { headers: { "Content-Type": "text/event-stream" } })
  }) as typeof fetch

  const events: string[] = []
  const client = new GuiClient("http://127.0.0.1:9130")
  const cursor = await client.subscribe("board-1", 7, (event) => {
    events.push(event.kind)
    return event.kind !== "job.complete"
  })

  expect(new URL(requests[0]!).searchParams.get("after")).toBe("7")
  expect(events).toEqual(["job.started", "job.complete"])
  expect(cursor).toBe(9)
})

test("subscribe accepts a bounded transient assistant delta", async () => {
  globalThis.fetch = (async (_input, _init) => new Response(
    "event: update\ndata: {\"kind\":\"assistant.delta\",\"message\":\"Assistant response update\",\"level\":\"info\",\"created_at\":\"now\",\"source\":\"agent\",\"sequence\":12,\"stream_id\":\"stream-1\",\"turn_id\":\"turn-1\",\"text\":\"Hello\",\"transient\":true}\n\n",
    { headers: { "Content-Type": "text/event-stream" } },
  )) as typeof fetch

  const received: string[] = []
  const client = new GuiClient("http://127.0.0.1:9130")
  const cursor = await client.subscribe("board-1", 11, (event) => {
    received.push(`${event.turn_id}:${event.text}`)
    return false
  })

  expect(received).toEqual(["turn-1:Hello"])
  expect(cursor).toBe(12)
})

test("subscribe rejects assistant deltas that are persistent or have invalid optional fields", async () => {
  globalThis.fetch = (async (_input, _init) => new Response(
    "event: update\ndata: {\"kind\":\"assistant.delta\",\"message\":\"Assistant response update\",\"level\":\"info\",\"created_at\":\"now\",\"source\":\"agent\",\"sequence\":12,\"stream_id\":\"stream-1\",\"turn_id\":\"turn-1\",\"text\":\"Hello\",\"transient\":false}\n\n",
    { headers: { "Content-Type": "text/event-stream" } },
  )) as typeof fetch

  const client = new GuiClient("http://127.0.0.1:9130")
  expect(client.subscribe("board-1", 11, () => undefined)).rejects.toThrow(
    "GUI event stream returned an invalid event",
  )
})
