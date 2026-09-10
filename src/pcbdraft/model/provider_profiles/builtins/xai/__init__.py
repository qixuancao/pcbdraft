"""xAI (Grok) provider profile."""

from pcbdraft.interfaces.tui import __version__ as _PCBDRAFT_VERSION
from pcbdraft.model.provider_profiles import register_provider
from pcbdraft.model.provider_profiles.base import ProviderProfile

xai = ProviderProfile(
    name="xai",
    aliases=("grok", "x-ai", "x.ai"),
    api_mode="codex_responses",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    auth_type="api_key",
    default_headers={"User-Agent": f"PCBDraft/{_PCBDRAFT_VERSION}"},
)

register_provider(xai)
