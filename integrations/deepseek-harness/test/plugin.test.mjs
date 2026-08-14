// Verifies the standalone DeepSeek Harness PCBDraft plugin.
// Run: node --test integrations/deepseek-harness/test/plugin.test.mjs
// Needs: Node.js 22+; no model, credentials, or DSH installation.

import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import test from 'node:test'

import { apply } from '../index.mjs'

function capabilities() {
  return {
    api_version: '1.0',
    methods: [
      'runtime.capabilities',
      'agent.request.prepare',
      'symbols.find',
      'agent.project.generate',
      'project.validate',
    ],
  }
}

function fakeContext() {
  const tools = []
  const sections = []
  return {
    ctx: {
      systemPrompt: { section: value => sections.push(value) },
      tools: { register: value => tools.push(value) },
    },
    tools,
    sections,
  }
}

test('registers the constrained PCBDraft PCB toolset', () => {
  const { ctx, tools, sections } = fakeContext()
  apply(ctx, { callRpc: async () => ({}) })

  assert.deepEqual(tools.map(tool => tool.name), [
    'pcb_prepare',
    'pcb_symbols',
    'pcb_generate',
  ])
  assert.match(sections[0].text, /without asking the user to choose layers/i)
})

test('pcb_prepare passes the agent-selected layer count to PCBDraft', async () => {
  const calls = []
  const { ctx, tools } = fakeContext()
  apply(ctx, {
    callRpc: async (method, params) => {
      calls.push({ method, params })
      if (method === 'runtime.capabilities') return capabilities()
      return { request: { board: { layers: params.layers } } }
    },
  })

  const tool = tools.find(value => value.name === 'pcb_prepare')
  const value = await tool.execute(
    {
      request_summary: 'Design a 6-layer SPI BME280 sensor board',
      design_name: 'BME280 sensor board',
      layers: 6,
      requested_parts: ['BME280'],
      functions: ['SPI sensor interface'],
    },
    { signal: new AbortController().signal },
  )

  assert.equal(value.ok, true)
  assert.deepEqual(calls, [
    {
      method: 'runtime.capabilities',
      params: {},
    },
    {
      method: 'agent.request.prepare',
      params: {
        request_summary: 'Design a 6-layer SPI BME280 sensor board',
        design_name: 'BME280 sensor board',
        layers: 6,
        requested_parts: ['BME280'],
        functions: ['SPI sensor interface'],
      },
    },
  ])
})

test('pcb_prepare does not impose a historical maximum layer count', async () => {
  const calls = []
  const { ctx, tools } = fakeContext()
  apply(ctx, {
    callRpc: async (method, params) => {
      calls.push({ method, params })
      if (method === 'runtime.capabilities') return capabilities()
      return { request: { board: { layers: params.layers } } }
    },
  })

  const tool = tools.find(value => value.name === 'pcb_prepare')
  const value = await tool.execute(
    {
      request_summary: 'Design a 65-layer test board',
      design_name: '65 layer board',
      layers: 65,
      requested_parts: [],
      functions: ['test interface'],
    },
    { signal: new AbortController().signal },
  )

  assert.equal(value.ok, true)
  assert.equal(calls[1].params.layers, 65)
})

test('pcb_generate confines the generated project to its configured workspace', async () => {
  const root = mkdtempSync(join(tmpdir(), 'pcbdraft-dsh-plugin-'))
  const workspace = join(root, 'workspace')
  const calls = []
  try {
    mkdirSync(join(root, '.venv', 'bin'), { recursive: true })
    writeFileSync(join(root, '.venv', 'bin', 'python'), '', 'utf8')
    const { ctx, tools } = fakeContext()
    apply(ctx, {
      pcbdraftRoot: root,
      workspace,
      callRpc: async (method, params) => {
        calls.push({ method, params })
        if (method === 'runtime.capabilities') return capabilities()
        if (method === 'agent.project.generate') return { root: params.output }
        if (method === 'project.validate') return { candidate_ready: true }
        throw new Error(`unexpected method: ${method}`)
      },
    })

    const tool = tools.find(value => value.name === 'pcb_generate')
    const value = await tool.execute(
      {
        request: { design_id: '../../external-output' },
        plan: { schema: 'pcbdraft-circuit-plan', version: 1 },
      },
      { signal: new AbortController().signal },
    )

    const output = calls[1].params.output
    assert.equal(value.ok, true)
    assert.deepEqual(calls.map(call => call.method), [
      'runtime.capabilities',
      'agent.project.generate',
      'project.validate',
    ])
    assert.equal(relative(workspace, output).startsWith('..'), false)
    assert.match(output, /projects\/external-output-/)
    assert.equal(calls[2].params.project, output)
    assert.equal(calls[1].params.retain_failed_attempt.startsWith(workspace), true)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('pcb_generate rejects repair attempts beyond the bounded two retries', async () => {
  const root = mkdtempSync(join(tmpdir(), 'pcbdraft-dsh-plugin-'))
  try {
    mkdirSync(join(root, '.venv', 'bin'), { recursive: true })
    writeFileSync(join(root, '.venv', 'bin', 'python'), '', 'utf8')
    const { ctx, tools } = fakeContext()
    apply(ctx, {
      pcbdraftRoot: root,
      workspace: join(root, 'workspace'),
      callRpc: async method => {
        if (method === 'runtime.capabilities') return capabilities()
        throw new Error(`unexpected method: ${method}`)
      },
    })
    const tool = tools.find(value => value.name === 'pcb_generate')
    const value = await tool.execute(
      {
        request: { design_id: 'bounded-repair' },
        plan: { schema: 'pcbdraft-circuit-plan', version: 1 },
        repair_attempt: 3,
      },
      { signal: new AbortController().signal },
    )
    assert.equal(value.ok, false)
    assert.match(value.summary, /rejected/i)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
