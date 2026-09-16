import type { Project } from "./bridge.ts"

export function initialProjectId(
  environment: Record<string, string | undefined> = process.env,
): string | null {
  const value = environment.PCBDRAFT_INITIAL_PROJECT_ID?.trim()
  return value || null
}

export function projectForReference(
  projects: readonly Project[],
  reference: string,
): Project | null {
  if (/^\d+$/.test(reference)) return projects[Number(reference) - 1] ?? null
  return projects.find((project) => project.id === reference) ?? null
}
