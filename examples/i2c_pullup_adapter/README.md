# I2C pull-up adapter

This stock-library example exercises a four-net bus, two connectors, and an
explicit pull-up path for both SDA and SCL. Generate and validate it from the
repository root:

    uv run pcbdraft agent-generate \
      examples/i2c_pullup_adapter/request.json \
      examples/i2c_pullup_adapter/circuit-plan.json \
      build/i2c-pullup-adapter
    uv run pcbdraft validate build/i2c-pullup-adapter

The deterministic preflight can confirm that both pull-up paths exist. It
cannot choose or qualify the resistor values against bus capacitance, speed,
device voltage limits, or pull-ups already present elsewhere.
