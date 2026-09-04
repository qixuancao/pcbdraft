# Third-party UI sources

The Web Workbench 2.0 implementation contains no copied third-party source
code, stylesheets, assets, fonts, or runtime dependencies. `NOTICE` therefore
does not need an additional attribution entry for this work.

The following are design and interaction references only; no files were copied
from them:

- DeepSeek Harness: workspace/session rail and command-palette interaction.
- NousResearch Hermes Agent and its Web Console: session-state hierarchy and
  bounded lifecycle feedback.
- KiCad/CAD/IDE workbenches and Linear-like information density: canvas-first
  layout, compact inspector, and progressive disclosure.

All implementation in `src/pcbdraft/web/` is original native HTML, CSS, and ES
module code authored for PCBDraft. No upstream commit is vendored or tracked as
a code-derived source.
