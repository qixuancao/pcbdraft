import { expect, test } from "bun:test"
import { initialProjectId, projectForReference } from "../src/startup.ts"
import type { Project } from "../src/bridge.ts"

const projects: Project[] = [
  { id: "board-a", name: "Board A", status: "generated" },
  { id: "board-b", name: "Board B", status: "draft" },
]

test("initialProjectId reads and trims the launcher environment", () => {
  expect(initialProjectId({ PCBDRAFT_INITIAL_PROJECT_ID: " board-b " })).toBe("board-b")
  expect(initialProjectId({ PCBDRAFT_INITIAL_PROJECT_ID: "  " })).toBeNull()
  expect(initialProjectId({})).toBeNull()
})

test("projectForReference resolves an exact ID or displayed list number", () => {
  expect(projectForReference(projects, "board-b")?.name).toBe("Board B")
  expect(projectForReference(projects, "1")?.id).toBe("board-a")
  expect(projectForReference(projects, "missing")).toBeNull()
})
