import { afterEach, expect, test } from "bun:test"
import { GuiClient } from "../src/bridge.ts"

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
