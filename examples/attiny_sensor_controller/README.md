# Legacy deterministic fixture: ATtiny402 + TMP102

This checked-in KiCad project is retained as a regression fixture for the
deterministic compiler, synchronization, validation, release, and
error-injection corpus. It is not the generic conversational product path and
must not be substituted for a user's named components.

It remains useful because it gives the low-level KiCad backend a stable,
independent test subject. Its past ERC/DRC and candidate evidence do not make it
a production sign-off, and they do not prove anything about a newly planned
generic board.

For current usage, see <a href="../../README.md">README.md</a> and the generic
<a href="../agent_plan_stm32_sht31">agent-plan fixture</a>.
