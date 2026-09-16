import { expect, test } from "bun:test"
import { resolveSlashCommand } from "../src/commands.ts"

test("unique command prefixes resolve case-insensitively", () => {
  expect(resolveSlashCommand("/res")).toBe("resume")
  expect(resolveSlashCommand("/RES")).toBe("resume")
  expect(resolveSlashCommand("/Resume")).toBe("resume")
})

test("ambiguous prefixes list every match instead of guessing", () => {
  expect(() => resolveSlashCommand("/res", ["resume", "reset", "help"])).toThrow(
    "Ambiguous command /res: /resume, /reset",
  )
})

test("an exact command wins even when another command shares its prefix", () => {
  expect(resolveSlashCommand("/resume", ["resume", "resume-all"])).toBe("resume")
})

test("unknown prefixes explain how to discover commands", () => {
  expect(() => resolveSlashCommand("/wat")).toThrow(
    "Unknown command /wat. Use /help for available commands.",
  )
})
