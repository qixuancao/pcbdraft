# Independent error corpus

`acceptance_requirements.json` is an independently authored low-voltage ATtiny402,
TMP102, I2C, and LED control-board requirement set. `error_corpus.json` applies one
isolated mutation per case and includes both faulty and clean controls.

No competitor fixture, output, or annotation was used to create the corpus. Cases
were chosen from generic electrical, part-contract, constraint, placement, routing,
manufacturing, identity, and supported-scope invariants. This makes the data useful
for runtime regression testing, but it is not a statistically representative sample
of all PCB failures and should not be presented as one.

Labels and expected rule codes are evaluation data. The optional model benchmark
does not include those labels in its prompt. See `LICENSE.md` for the CC0 dedication.
