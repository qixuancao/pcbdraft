"""Project verification subsystem.

Ported from superagent-ai/grok-cli's verify subsystem (scoped):
static run-recipe detection, a persisted environment manifest, and a
smoke-test runner used by ``python -m pcbdraft.agent.verify``.

Sources:
- https://github.com/superagent-ai/grok-cli/blob/main/src/verify/recipes.ts
- https://github.com/superagent-ai/grok-cli/blob/main/src/verify/environment.ts
"""

from pcbdraft.agent.verify.environment import (
    load_manifest,
    load_or_detect,
    manifest_path,
    save_manifest,
)
from pcbdraft.agent.verify.recipes import Recipe, detect_package_manager, detect_recipe
from pcbdraft.agent.verify.runner import (
    PhaseResult,
    ReadinessResult,
    VerifyResult,
    run_verify,
)

__all__ = [
    "PhaseResult",
    "ReadinessResult",
    "Recipe",
    "VerifyResult",
    "detect_package_manager",
    "detect_recipe",
    "load_manifest",
    "load_or_detect",
    "manifest_path",
    "run_verify",
    "save_manifest",
]
