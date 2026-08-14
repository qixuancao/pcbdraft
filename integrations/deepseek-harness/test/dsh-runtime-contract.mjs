// Verify PCBDraft's plugin against the real DeepSeek Harness tool runtime.
// Run: node integrations/deepseek-harness/test/dsh-runtime-contract.mjs
// Needs: a built DeepSeek Harness checkout at $DSH_ROOT or /mnt/2T/deepseek-harness.

import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { join, resolve } from 'node:path'

import { apply } from '../index.mjs'

const dshRoot = resolve(process.env.DSH_ROOT || '/mnt/2T/deepseek-harness')
const dshRequire = createRequire(join(dshRoot, 'apps', 'cli', 'package.json'))
const { Context } = await import(dshRequire.resolve('@deepseek-ai/cordis'))
const { default: SystemPrompt } = await import(
  dshRequire.resolve('@deepseek-ai/dsh-system-prompt'),
)
const { default: ToolRuntime } = await import(dshRequire.resolve('@deepseek-ai/dsh-tools'))
const { CallId } = await import(dshRequire.resolve('@deepseek-ai/dsh-llm'))

const ctx = new Context()
await ctx.plugin(SystemPrompt)
await ctx.plugin(ToolRuntime)
apply(ctx, {
  callRpc: async (method, params) => {
    if (method === 'runtime.capabilities') {
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
    assert.equal(method, 'agent.request.prepare')
    return { request: { board: { layers: params.layers } } }
  },
})

const toolNames = ctx.tools.schemas().map(tool => tool.name)
assert.deepEqual(toolNames, ['pcb_prepare', 'pcb_symbols', 'pcb_generate'])
const result = await ctx.tools.execute({
  signal: new AbortController().signal,
  callId: CallId('pcbdraft-dsh-contract'),
  name: 'pcb_prepare',
  arguments: {
    request_summary: 'Design a 6-layer 3.3 V SPI BME280 sensor board',
    design_name: 'BME280 sensor board',
    layers: 6,
    requested_parts: ['BME280'],
    functions: ['SPI sensor interface'],
  },
})
assert.equal(result.isError, false)
assert.equal(result.value.ok, true)
assert.equal(result.value.data.request.board.layers, 6)
const assembly = await ctx.systemPrompt.assemble()
assert.ok(assembly.sections.some(section => section.name === 'pcbdraft:pcb-agent'))

console.log(JSON.stringify({
  tools: toolNames,
  layer: result.value.data.request.board.layers,
  prompt: true,
}))
