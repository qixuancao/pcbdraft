export const SLASH_COMMANDS = ["resume", "open", "stop", "help", "quit", "exit"] as const

export type SlashCommand = (typeof SLASH_COMMANDS)[number]

export class SlashCommandError extends Error {}

export function resolveSlashCommand(
  token: string,
  commands: readonly string[] = SLASH_COMMANDS,
): string {
  if (!token.startsWith("/")) throw new SlashCommandError(`Not a slash command: ${token}`)
  const prefix = token.slice(1).toLocaleLowerCase()
  const exact = commands.find((command) => command.toLocaleLowerCase() === prefix)
  if (exact) return exact
  const matches = commands.filter((command) => command.toLocaleLowerCase().startsWith(prefix))
  if (matches.length === 1) return matches[0]!
  if (matches.length === 0) throw new SlashCommandError(`Unknown command ${token}. Use /help for available commands.`)
  throw new SlashCommandError(`Ambiguous command ${token}: ${matches.map((command) => `/${command}`).join(", ")}`)
}
