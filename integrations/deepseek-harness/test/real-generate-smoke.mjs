// Smoke-test the actual DSH plugin -> PCBDraft -> KiCad generation path.
// Run: node integrations/deepseek-harness/test/real-generate-smoke.mjs
// Needs: Node.js, PCBDraft .venv, local KiCad; no model or credential.

import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { apply } from '../index.mjs'

const moduleRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const pcbdraftRoot = resolve(process.env.PCBDRAFT_ROOT || moduleRoot)
const workspace = resolve(
  process.env.PCBDRAFT_DSH_SMOKE_WORKSPACE
    || join(pcbdraftRoot, '.dsh-workspace', 'smoke'),
)

if (!existsSync(join(pcbdraftRoot, '.venv', 'bin', 'python'))) {
  throw new Error('PCBDraft .venv is unavailable')
}

function fakeContext() {
  const tools = []
  return {
    ctx: {
      systemPrompt: { section: () => {} },
      tools: { register: tool => tools.push(tool) },
    },
    tools,
  }
}

function ledPlan(designId) {
  return {
    schema: 'pcbdraft-circuit-plan',
    version: 1,
    design_id: designId,
    summary: 'A generic connector, series resistor, and LED topology.',
    assumptions: ['The external source is regulated to 3.3 V.'],
    notes: ['LED current and resistor value require human review.'],
    components: [
      {
        id: 'capacitor', reference: 'C1', symbol: 'Device:C', value: '100n',
        role: 'supply_bypass', footprint: 'Capacitor_SMD:C_0603_1608Metric',
        on_board: true, exact_name: null,
      },
      {
        id: 'input', reference: 'J1', symbol: 'Connector_Generic:Conn_01x02',
        value: 'POWER', role: 'power_input_connector',
        footprint: 'Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal',
        on_board: true, exact_name: null,
      },
      {
        id: 'resistor', reference: 'R1', symbol: 'Device:R', value: '1k',
        role: 'led_current_limit', footprint: 'Resistor_SMD:R_0603_1608Metric',
        on_board: true, exact_name: null,
      },
      {
        id: 'led', reference: 'D1', symbol: 'Device:LED', value: 'LED',
        role: 'indicator', footprint: 'LED_SMD:LED_0603_1608Metric',
        on_board: true, exact_name: null,
      },
    ],
    nets: [
      {
        id: 'gnd', name: 'GND', net_class: 'power', intent: 'Common return.',
        endpoints: [
          { component: 'capacitor', pin: '2', role: 'return' },
          { component: 'input', pin: '2', role: 'return' },
          { component: 'led', pin: '1', role: 'return' },
        ],
      },
      {
        id: 'v3v3', name: '3V3', net_class: 'power', intent: 'External regulated source.',
        endpoints: [
          { component: 'capacitor', pin: '1', role: 'load' },
          { component: 'input', pin: '1', role: 'source' },
          { component: 'resistor', pin: '1', role: 'load' },
        ],
      },
      {
        id: 'led_a', name: 'LED_A', net_class: 'signal', intent: 'Current-limited LED anode.',
        endpoints: [
          { component: 'resistor', pin: '2', role: 'load' },
          { component: 'led', pin: '2', role: 'load' },
        ],
      },
    ],
  }
}

const { ctx, tools } = fakeContext()
apply(ctx, { pcbdraftRoot, workspace })
const tool = name => {
  const result = tools.find(candidate => candidate.name === name)
  assert.ok(result, `missing ${name}`)
  return result
}
const signal = new AbortController().signal
const prepared = await tool('pcb_prepare').execute(
  {
    request_summary: 'A low-voltage LED indicator board with a two-pin 3.3 V power input.',
    design_name: 'DSH smoke LED indicator',
    layers: 2,
    requested_parts: [],
    functions: ['status indicator'],
  },
  { signal },
)
assert.equal(prepared.ok, true, prepared.summary)
const symbols = await tool('pcb_symbols').execute({ query: 'LED', limit: 3 }, { signal })
assert.equal(symbols.ok, true, symbols.summary)
const request = prepared.data.request
const generated = await tool('pcb_generate').execute(
  { request, plan: ledPlan(request.design_id) },
  { signal },
)
assert.equal(generated.ok, true, generated.summary)

if (generated.data.existing) {
  assert.ok(existsSync(generated.data.project.root), 'existing project is missing')
  console.log(JSON.stringify({
    status: 'reused',
    project: generated.data.project.root,
    design_id: request.design_id,
  }))
} else {
  assert.equal(generated.data.validation.candidate_ready, true)
  console.log(JSON.stringify({
    status: 'generated',
    project: generated.data.generated.root,
    design_id: request.design_id,
    candidate_ready: generated.data.validation.candidate_ready,
    production_ready: generated.data.validation.production_ready,
  }))
}
