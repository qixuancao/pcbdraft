# Basic stock KiCad board

This example uses only symbols and footprints shipped with KiCad: a connector,
capacitor, resistor, and LED. Generate the native project from the repository
root:

    uv run pcbdraft agent-generate \
      examples/basic_stock_board/request.json \
      examples/basic_stock_board/circuit-plan.json \
      build/basic-stock-board

The output directory contains `.kicad_sch`, `.kicad_pcb`, and `.kicad_pro`
files plus PCBDraft's editable request, plan, and semantic IR. On KiCad
10.0.5 this example completes routing and has zero ERC violations, DRC
violations, unconnected items, and schematic-parity errors.

That result only describes the checks that ran. It does not validate LED
current, electrical safety, regulatory compliance, or manufacturing fitness.
