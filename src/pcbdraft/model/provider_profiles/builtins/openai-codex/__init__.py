"""OpenAI Codex (Responses API) provider profile."""

from pcbdraft.model.provider_profiles import register_provider
from pcbdraft.model.provider_profiles.base import ProviderProfile

openai_codex = ProviderProfile(
    name="openai-codex",
    aliases=("codex", "openai_codex"),
    api_mode="codex_responses",
    env_vars=(),  # OAuth external — no API key
    base_url="https://chatgpt.com/backend-api/codex",
    auth_type="oauth_external",
)

register_provider(openai_codex)
