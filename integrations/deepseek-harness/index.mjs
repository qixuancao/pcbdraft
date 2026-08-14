// Constrained DeepSeek Harness plugin for PCBDraft PCB generation.
// Run tests: node --test integrations/deepseek-harness/test/plugin.test.mjs
// Needs: Node.js 22+, a configured PCBDraft root, and DeepSeek Harness.

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'pcbdraft-pcb'
export const inject = ['tools', 'systemPrompt']

const MODULE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const MAX_RPC_OUTPUT_BYTES = 4 * 1024 * 1024
const MAX_RENDER_CHARS = 256 * 1024
const MAX_TEXT_BYTES = 8 * 1024
const MAX_LIST_ITEMS = 32
const MAX_REPAIR_ATTEMPTS = 2
const REQUIRED_API_MAJOR = '1'
const REQUIRED_RPC_METHODS = [
  'runtime.capabilities',
  'agent.request.prepare',
  'symbols.find',
  'agent.project.generate',
  'project.validate',
]

class RpcRejectedError extends Error {
  constructor(message, details = {}) {
    super(message)
    this.details = details
  }
}
class RpcConfigurationError extends Error {}
class RpcAbortedError extends Error {}

/** Register only structured PCB planning and generation tools. */
export function apply(ctx, config = {}) {
  if (!ctx?.tools?.register || !ctx?.systemPrompt?.section) {
    throw new TypeError('PCBDraft requires DeepSeek Harness tools and systemPrompt services')
  }
  const callRpc = typeof config.callRpc === 'function'
    ? config.callRpc
    : createRpcClient(config)
  const ensureCompatible = createCompatibilityGuard(callRpc)

  ctx.systemPrompt.section({
    name: 'pcbdraft:pcb-agent',
    order: 120,
    text: [
      'You are the PCBDraft PCB planning agent.',
      'Turn the user’s ordinary-language board request into a constrained, reviewable KiCad generation attempt.',
      'Choose a practical layer count yourself without asking the user to choose layers; preserve an explicit layer count when the user gives one.',
      'Use pcb_prepare first, inspect pcb_symbols when needed, then use pcb_generate with the returned request and a plan that conforms to its plan_schema.',
      'Call pcb_generate with repair_attempt 0 first. If it returns ok=false, revise the complete semantic plan from its structured deterministic evidence and retry with 1, then at most 2. Never make a third repair attempt.',
      'Use only installed stock KiCad symbols and footprints returned by PCBDraft. Do not invent vendor libraries, package names, datasheet facts, raw KiCad text, coordinates, shell commands, or files.',
      'Do not reject a request merely because it mentions high frequency, high power, RF, mains, medical, aerospace, or another demanding domain. Make a bounded generation attempt and report the actual validation evidence and gaps.',
      'A generated project is a candidate for engineering review, not proof of electrical, safety, compliance, or manufacturing fitness.',
    ].join('\n'),
  })

  ctx.tools.register(prepareTool(callRpc, ensureCompatible))
  ctx.tools.register(symbolsTool(callRpc, ensureCompatible))
  ctx.tools.register(generateTool(callRpc, config, ensureCompatible))
}

function prepareTool(callRpc, ensureCompatible) {
  return toolDefinition({
    name: 'pcb_prepare',
    description: 'Create a constrained PCBDraft board request and obtain stock KiCad symbol context plus the exact circuit-plan schema. Choose the layer count before calling this tool.',
    parameters: objectSchema({
      request_summary: stringSchema('Complete user board request.', true),
      design_name: stringSchema('Short human-readable board name.', true),
      layers: integerSchema('Agent-selected copper layer count.', true, 1),
      requested_parts: stringArraySchema('Explicitly requested ICs, connectors, or parts.', true),
      functions: stringArraySchema('Board functions and interfaces.', true),
    }),
    async execute(args, exec) {
      return guarded(async () => {
        await ensureCompatible(exec?.signal)
        const params = normalizePrepareArgs(args)
        const result = await callRpc('agent.request.prepare', params, exec?.signal)
        return succeeded('PCB request prepared.', result)
      })
    },
  })
}

function symbolsTool(callRpc, ensureCompatible) {
  return toolDefinition({
    name: 'pcb_symbols',
    description: 'Search only symbols installed in the local KiCad libraries. Use this before selecting an uncertain component.',
    parameters: objectSchema({
      query: stringSchema('Part, connector, or function to search in local KiCad libraries.', true),
      limit: integerSchema('Maximum candidates to return; defaults to 12.', false, 1, 32),
    }),
    async execute(args, exec) {
      return guarded(async () => {
        await ensureCompatible(exec?.signal)
        const value = expectObject(args, 'tool arguments')
        const params = { query: expectText(value.query, 'query') }
        if (value.limit !== undefined) params.limit = expectInteger(value.limit, 'limit', 1, 32)
        const result = await callRpc('symbols.find', params, exec?.signal)
        return succeeded('Local KiCad symbol search completed.', result)
      })
    },
  })
}

function generateTool(callRpc, config, ensureCompatible) {
  return toolDefinition({
    name: 'pcb_generate',
    description: 'Generate a native KiCad project from a prepared request and a schema-conforming semantic circuit plan, then run PCBDraft validation. The output location is controlled by PCBDraft.',
    parameters: objectSchema({
      request: jsonObjectSchema('The request object returned by pcb_prepare.', true),
      plan: jsonObjectSchema('A high-level circuit plan conforming to pcb_prepare plan_schema.', true),
      repair_attempt: integerSchema('Zero for the first generation call; one or two only after structured failure evidence.', false, 0, MAX_REPAIR_ATTEMPTS),
    }),
    async execute(args, exec) {
      return guarded(async () => {
        await ensureCompatible(exec?.signal)
        const value = expectObject(args, 'tool arguments')
        const request = expectObject(value.request, 'request')
        const plan = expectObject(value.plan, 'plan')
        const repairAttempt = value.repair_attempt === undefined
          ? 0
          : expectInteger(value.repair_attempt, 'repair_attempt', 0, MAX_REPAIR_ATTEMPTS)
        const output = projectOutputPath(config, request, plan)
        const retainedAttempt = retainedAttemptPath(config, output)

        if (existsSync(output)) {
          const existing = await callRpc('project.inspect', { project: output }, exec?.signal)
          return succeeded('Matching generation already exists; it was not overwritten.', {
            existing: true,
            repair_attempt: repairAttempt,
            project: existing,
          })
        }

        let generated
        try {
          generated = await callRpc(
            'agent.project.generate',
            { request, plan, output, retain_failed_attempt: retainedAttempt },
            exec?.signal,
          )
        } catch (error) {
          if (error instanceof RpcRejectedError) {
            error.details = {
              ...error.details,
              repair_attempt: repairAttempt,
              retained_attempt: retainedAttempt,
            }
          }
          throw error
        }
        let validation
        try {
          validation = await callRpc(
            'project.validate',
            { project: output, output: join(output, 'validation') },
            exec?.signal,
          )
        } catch (error) {
          if (!(error instanceof RpcRejectedError)) throw error
          validation = {
            attempted: true,
            completed: false,
            note: 'Generation completed, but PCBDraft validation did not finish. Inspect the generated project before retrying.',
          }
        }
        return succeeded('KiCad generation attempt completed.', {
          repair_attempt: repairAttempt,
          generated,
          validation,
        })
      })
    },
  })
}

function toolDefinition({ name: toolName, description, parameters, execute }) {
  return {
    name: toolName,
    description,
    parameters,
    output: {
      schema: objectSchema({
        ok: booleanSchema('Whether the PCBDraft operation completed.', true),
        summary: stringSchema('Short operation result.', true),
        data: jsonObjectSchema('Structured PCBDraft result.', true),
      }),
      render: (_args, value) => [{ type: 'text', text: renderResult(value) }],
    },
    timeoutMs: 180_000,
    execute,
  }
}

function objectSchema(properties) {
  const required = Object.entries(properties)
    .filter(([, value]) => value.required === true)
    .map(([key]) => key)
  const normalized = Object.fromEntries(
    Object.entries(properties).map(([key, value]) => {
      const { required: _required, ...schema } = value
      return [key, schema]
    }),
  )
  return {
    type: 'object',
    additionalProperties: false,
    ...(required.length > 0 ? { required } : {}),
    properties: normalized,
  }
}

function stringSchema(description, required) {
  return { type: 'string', description, ...(required ? { required: true } : {}) }
}

function integerSchema(description, required, minimum, maximum) {
  return {
    type: 'integer',
    description,
    minimum,
    ...(maximum === undefined ? {} : { maximum }),
    ...(required ? { required: true } : {}),
  }
}

function booleanSchema(description, required) {
  return { type: 'boolean', description, ...(required ? { required: true } : {}) }
}

function stringArraySchema(description, required) {
  return {
    type: 'array',
    description,
    maxItems: MAX_LIST_ITEMS,
    items: { type: 'string' },
    ...(required ? { required: true } : {}),
  }
}

function jsonObjectSchema(description, required) {
  return {
    type: 'object',
    description,
    additionalProperties: true,
    ...(required ? { required: true } : {}),
  }
}

function normalizePrepareArgs(args) {
  const value = expectObject(args, 'tool arguments')
  return {
    request_summary: expectText(value.request_summary, 'request_summary'),
    design_name: expectText(value.design_name, 'design_name'),
    layers: expectInteger(value.layers, 'layers', 1),
    requested_parts: expectTextList(value.requested_parts, 'requested_parts'),
    functions: expectTextList(value.functions, 'functions'),
  }
}

function createCompatibilityGuard(callRpc) {
  let compatible
  return async signal => {
    if (compatible) return compatible
    compatible = Promise.resolve(callRpc('runtime.capabilities', {}, signal))
      .then(validateCapabilities)
      .catch(error => {
        compatible = undefined
        throw error
      })
    return compatible
  }
}

function validateCapabilities(value) {
  const capabilities = expectObject(value, 'PCBDraft capabilities')
  const version = expectText(capabilities.api_version, 'api_version')
  if (version.split('.')[0] !== REQUIRED_API_MAJOR) {
    throw new RpcConfigurationError(`PCBDraft API ${version} is incompatible`)
  }
  if (!Array.isArray(capabilities.methods)) {
    throw new RpcConfigurationError('PCBDraft capabilities do not list methods')
  }
  const missing = REQUIRED_RPC_METHODS.filter(method => !capabilities.methods.includes(method))
  if (missing.length > 0) {
    throw new RpcConfigurationError(`PCBDraft API is missing required methods: ${missing.join(', ')}`)
  }
  return { api_version: version }
}

function expectObject(value, name) {
  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    throw new RpcRejectedError(`${name} must be an object`)
  }
  return value
}

function expectText(value, name) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new RpcRejectedError(`${name} must be a non-empty string`)
  }
  if (Buffer.byteLength(value, 'utf8') > MAX_TEXT_BYTES) {
    throw new RpcRejectedError(`${name} exceeds the byte limit`)
  }
  return value.trim()
}

function expectTextList(value, name) {
  if (!Array.isArray(value) || value.length > MAX_LIST_ITEMS) {
    throw new RpcRejectedError(`${name} must be an array with at most ${MAX_LIST_ITEMS} items`)
  }
  const normalized = value.map((item, index) => expectText(item, `${name}[${index}]`))
  if (new Set(normalized).size !== normalized.length) {
    throw new RpcRejectedError(`${name} cannot contain duplicate items`)
  }
  return normalized
}

function expectInteger(value, name, minimum, maximum) {
  if (
    !Number.isInteger(value)
    || value < minimum
    || (maximum !== undefined && value > maximum)
  ) {
    const range = maximum === undefined
      ? `at least ${minimum}`
      : `from ${minimum} to ${maximum}`
    throw new RpcRejectedError(`${name} must be an integer ${range}`)
  }
  return value
}

function projectOutputPath(config, request, plan) {
  const root = pcbdraftRoot(config)
  const workspace = configuredWorkspace(config, root)
  const designId = typeof request.design_id === 'string' ? request.design_id : 'board'
  const fingerprint = createHash('sha256')
    .update(stableJson({ request, plan }))
    .digest('hex')
    .slice(0, 16)
  const output = resolve(workspace, 'projects', `${safeSegment(designId)}-${fingerprint}`)
  ensureInside(workspace, output)
  mkdirSync(dirname(output), { recursive: true })
  return output
}

function retainedAttemptPath(config, output) {
  const root = pcbdraftRoot(config)
  const workspace = configuredWorkspace(config, root)
  const attempt = resolve(workspace, 'attempts', basename(output))
  ensureInside(workspace, attempt)
  mkdirSync(dirname(attempt), { recursive: true })
  return attempt
}

function pcbdraftRoot(config) {
  const candidate = typeof config.pcbdraftRoot === 'string'
    ? config.pcbdraftRoot
    : (process.env.PCBDRAFT_ROOT || (existsSync(join(MODULE_ROOT, '.venv', 'bin', 'python')) ? MODULE_ROOT : ''))
  if (!candidate || !isAbsolute(candidate)) {
    throw new RpcConfigurationError('PCBDraft root is not configured')
  }
  const root = resolve(candidate)
  if (!existsSync(join(root, '.venv', 'bin', 'python'))) {
    throw new RpcConfigurationError('PCBDraft virtual environment is unavailable')
  }
  return root
}

function configuredWorkspace(config, root) {
  const candidate = typeof config.workspace === 'string'
    ? config.workspace
    : join(root, '.dsh-workspace')
  if (!isAbsolute(candidate)) {
    throw new RpcConfigurationError('PCBDraft workspace must be an absolute path')
  }
  const workspace = resolve(candidate)
  mkdirSync(workspace, { recursive: true })
  return workspace
}

function ensureInside(parent, child) {
  const path = relative(parent, child)
  if (path === '..' || path.startsWith('../') || path.startsWith('..\\') || isAbsolute(path)) {
    throw new RpcConfigurationError('PCBDraft output escaped its configured workspace')
  }
}

function safeSegment(value) {
  const result = String(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return (result || 'board').slice(0, 96)
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function createRpcClient(config) {
  return async (method, params, signal) => {
    const root = pcbdraftRoot(config)
    const result = await runRpc({
      root,
      method,
      params,
      signal,
    })
    return result
  }
}

function runRpc({ root, method, params, signal }) {
  if (signal?.aborted) return Promise.reject(new RpcAbortedError('operation cancelled'))
  const python = join(root, '.venv', 'bin', 'python')
  const request = JSON.stringify({ jsonrpc: '2.0', id: 'pcbdraft-dsh', method, params })
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(python, ['-m', 'pcbdraft', 'api'], {
      cwd: root,
      detached: process.platform !== 'win32',
      env: safeEnvironment(),
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    let stdout = Buffer.alloc(0)
    let stdoutOverflow = false
    let settled = false

    const finish = (callback, value) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abort)
      callback(value)
    }
    const terminate = () => {
      if (child.pid && process.platform !== 'win32') {
        try {
          process.kill(-child.pid, 'SIGTERM')
          return
        } catch {}
      }
      child.kill('SIGTERM')
    }
    const abort = () => {
      terminate()
      finish(rejectPromise, new RpcAbortedError('operation cancelled'))
    }

    signal?.addEventListener('abort', abort, { once: true })
    child.on('error', () => finish(rejectPromise, new RpcConfigurationError('PCBDraft API could not start')))
    child.stdout.on('data', chunk => {
      if (settled) return
      stdout = Buffer.concat([stdout, Buffer.from(chunk)])
      if (stdout.length > MAX_RPC_OUTPUT_BYTES) {
        stdoutOverflow = true
        terminate()
      }
    })
    child.stderr.on('data', () => {})
    child.on('close', code => {
      if (settled) return
      if (stdoutOverflow) {
        finish(rejectPromise, new RpcRejectedError('PCBDraft API response exceeded its limit'))
        return
      }
      if (code !== 0) {
        finish(rejectPromise, new RpcRejectedError('PCBDraft API exited unsuccessfully'))
        return
      }
      let response
      try {
        response = JSON.parse(stdout.toString('utf8').trim())
      } catch {
        finish(rejectPromise, new RpcRejectedError('PCBDraft API returned an invalid response'))
        return
      }
      if (response?.error || !Object.hasOwn(response ?? {}, 'result')) {
        finish(rejectPromise, new RpcRejectedError(
          'PCBDraft rejected the request',
          normalizeRpcError(response?.error),
        ))
        return
      }
      finish(resolvePromise, response.result)
    })
    child.stdin.end(`${request}\n`)
  })
}

function normalizeRpcError(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return { category: 'rpc_rejected' }
  }
  const details = { category: 'rpc_rejected' }
  if (Number.isInteger(value.code)) details.code = value.code
  if (typeof value.message === 'string' && value.message.trim()) {
    details.message = value.message.replace(/\s+/g, ' ').trim().slice(0, 2048)
  }
  if (value.data !== null && typeof value.data === 'object' && !Array.isArray(value.data)) {
    if (typeof value.data.type === 'string') details.type = value.data.type.slice(0, 160)
  }
  return details
}

function safeEnvironment() {
  const names = [
    'HOME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'XDG_CACHE_HOME',
    'XDG_CONFIG_HOME', 'TMPDIR', 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY',
  ]
  const environment = { PYTHONNOUSERSITE: '1' }
  for (const name of names) {
    if (process.env[name] !== undefined) environment[name] = process.env[name]
  }
  return environment
}

async function guarded(operation) {
  try {
    return await operation()
  } catch (error) {
    if (error instanceof RpcAbortedError) return failed('PCBDraft operation cancelled.')
    if (error instanceof RpcConfigurationError) return failed('PCBDraft PCB runtime is not configured.')
    if (error instanceof RpcRejectedError) {
      return failed('PCBDraft rejected the structured PCB request or plan.', error.details)
    }
    return failed('PCBDraft PCB operation failed.')
  }
}

function succeeded(summary, data) {
  return { ok: true, summary, data: asData(data) }
}

function failed(summary, data = {}) {
  return { ok: false, summary, data: asData(data) }
}

function asData(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value
    : { value }
}

function renderResult(value) {
  try {
    const body = JSON.stringify(value.data)
    const text = body.length > 0 ? `${value.summary}\n${body}` : value.summary
    return text.length <= MAX_RENDER_CHARS
      ? text
      : `${text.slice(0, MAX_RENDER_CHARS)}\n[truncated by PCBDraft]`
  } catch {
    return value?.summary || 'PCBDraft tool completed.'
  }
}
