"""Read old embedded skill metadata without accessing another application's home.

New producers write metadata.pcbdraft. The agent-owned compatibility reader
preserves unknown legacy fields while native values (including empty ones) win.
"""

from __future__ import annotations

from typing import Any


def read_pcbdraft_metadata(metadata: Any) -> dict:
    """Adapt tools' metadata-block API to the agent-owned frontmatter reader."""
    from pcbdraft.agent.legacy_compat import read_skill_metadata

    return read_skill_metadata({"metadata": metadata})
