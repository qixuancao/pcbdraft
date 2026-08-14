# Generic agent-plan fixture

This is a generic semantic-plan fixture, not a product profile and not a
complete STM32 reference design. It demonstrates the shape produced by a
planner after it has selected actual symbols and pins from the local KiCad
installation.

The plan intentionally lacks power-tree, decoupling, reset/boot/debug, I2C
pull-up, connector, and layout constraints. It should therefore be reviewed,
expanded, or fail — never be called a usable board merely because its two
named parts resolve.

Compile it:

    uv run copperwright agent-compile request.json circuit-plan.json \
      --ir-output /tmp/generic.pcbir.json \
      --parts-output /tmp/generic.parts.json --json

Then make an inspectable native attempt:

    uv run copperwright agent-generate request.json circuit-plan.json \
      /tmp/generic-stm32-sht31 --json

On a bounded router this incomplete plan may fail after the native schematic is
created. The CLI reports the sibling retained attempt directory; inspect it for
the request, plan, IR, extracted parts, native staging, and exact failure.
