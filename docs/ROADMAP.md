# PCBDraft roadmap

This roadmap separates a usable open-source MVP from the much larger claim of
autonomous production PCB engineering. It contains no fixed board catalog and no
arbitrary layer ceiling: the agent selects a practical stackup when the user does
not, while the installed KiCad backend reports concrete technical limits.

## Current MVP baseline

The current runtime can accept an ordinary-language request through a Codex-like
Python TUI, create a schema-constrained circuit plan, resolve installed stock
KiCad symbols and footprints, generate native schematic/PCB/project files, make
a bounded deterministic placement/routing attempt, run available KiCad and
PCBDraft checks, repair at most twice, and preserve inspectable evidence.

Release acceptance currently includes three materially different small boards:

- a connector, bypass capacitor, resistor, and LED indicator;
- a two-connector passive RC low-pass breakout; and
- a two-connector I2C adapter with explicit SDA/SCL pull-ups.

All three complete real KiCad routing, ERC, DRC, schematic parity, and the
candidate gate in the supported test environment. They remain non-production
because component qualification, engineering review, fabrication, and physical
test evidence are external. An intentionally incomplete STM32F405/SHT31 plan
also completes its declared fine-pitch routes, then fails the correct electrical
gates for missing power pins, rail source, decoupling, and pull-ups.

## Next: broader useful prototypes

Priority work should improve generality rather than add named-board branches:

1. Add semantic constraints for connector pinout, net labels, mounting holes,
   board outline intent, placement regions, current classes, differential pairs,
   and keepouts without granting the model raw coordinates or KiCad text.
2. Expand deterministic circuit review for regulator feedback, reset/boot/debug,
   protection, crystal/clock networks, USB basics, pull direction, analog bias,
   and device-specific power-pin families.
3. Add a reviewed component-evidence workflow that can import exact manufacturer
   identity, datasheet revision, package/pin contract, ratings, lifecycle, and
   footprint qualification without treating web/model text as verified data.
4. Feed structured placement/routing congestion back into bounded replanning,
   and improve orientation, functional grouping, fan-out, plane assignment, and
   rip-up/retry while keeping search limits and deterministic receipts.
5. Add TUI-native schematic/board preview navigation, clearer semantic diffs,
   per-finding remediation, and an explicit handoff to KiCad for manual edits.

Acceptance for this milestone should use a growing public corpus of small boards,
report generation/routing/gate rates by topology, and retain every failure class.
Passing the corpus must never be restated as arbitrary-board support.

## Later: engineering candidate workflow

An engineering-candidate release needs reproducible library/version locking,
vendor capability profiles, BOM alternatives, tolerance and power analysis,
simulation adapters where meaningful, richer DFM checks, and attributed human
review. Hardware-in-the-loop fixtures should fabricate, assemble, bring up, and
measure a small open test corpus so L7 represents physical evidence rather than a
software fixture.

## Explicit non-goals and release rule

PCBDraft should not become a second EDA editor, let a model mutate raw native
files, silently replace a requested circuit with a demo, self-sign engineering
review, or infer production readiness from ERC/DRC alone. New claims ship only
when a named artifact and reproducible test support them; unavailable sourcing,
simulation, human review, fabrication, or measurement remains unavailable.
