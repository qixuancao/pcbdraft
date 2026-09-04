"""Kilo Code provider profile."""

from pcbdraft.model.provider_profiles import register_provider
from pcbdraft.model.provider_profiles.base import ProviderProfile

kilocode = ProviderProfile(
    name="kilocode",
    aliases=("kilo-code", "kilo", "kilo-gateway"),
    env_vars=("KILOCODE_API_KEY",),
    base_url="https://api.kilo.ai/api/gateway",
    default_aux_model="google/gemini-3.6-flash",
)

register_provider(kilocode)
