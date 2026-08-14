# Generic agent-plan fixture

This is a generic semantic-plan fixture, not a product profile and not a
complete STM32 reference design. It demonstrates the shape produced by a
planner after it has selected actual symbols and pins from the local KiCad
installation.

The plan intentionally lacks power-tree, decoupling, reset/boot/debug, I2C
pull-up, connector, and layout constraints. It should therefore be reviewed
and expanded — never called a usable board merely because its two named parts
resolve and the bounded router can connect the four declared nets.

Compile it:

    uv run pcbdraft agent-compile request.json circuit-plan.json \
      --ir-output /tmp/generic.pcbir.json \
      --parts-output /tmp/generic.parts.json --json

Then make an inspectable native attempt:

    uv run pcbdraft agent-generate request.json circuit-plan.json \
      /tmp/generic-stm32-sht31 --json

The current bounded router completes this fixture's fine-pitch fan-out and
retains a native project. Run `pcbdraft validate` on the result:
deterministic power-pin, rail-source, decoupling, I2C pull-up, ERC, and parity
gates reject it as a candidate. This fixture separates geometric progress from
electrical completeness on purpose.
