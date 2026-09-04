"""Arcee AI provider profile."""

from pcbdraft.model.provider_profiles import register_provider
from pcbdraft.model.provider_profiles.base import ProviderProfile

arcee = ProviderProfile(
    name="arcee",
    aliases=("arcee-ai", "arceeai"),
    env_vars=("ARCEEAI_API_KEY",),
    base_url="https://api.arcee.ai/api/v1",
)

register_provider(arcee)
