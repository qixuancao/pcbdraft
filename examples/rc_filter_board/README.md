# Passive RC filter breakout

This stock-library example exercises a passive analog topology with two
connectors, one series resistor, and one shunt capacitor. Generate and validate
it from the repository root:

    uv run pcbdraft agent-generate \
      examples/rc_filter_board/request.json \
      examples/rc_filter_board/circuit-plan.json \
      build/rc-filter-board
    uv run pcbdraft validate build/rc-filter-board

Successful generation proves only that the retained topology, native KiCad
files, bounded routing, and available checks completed. Review the selected RC
values, impedances, bandwidth, and connector assignment before fabrication.
