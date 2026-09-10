"""Product attribution and identities registered with third-party services.

OAuth client IDs are service registrations, not product branding. The Nous
Portal registration below must remain unchanged until an actual PCBDraft
registration is issued. Provider configuration may explicitly override it.
DeepInfra's catalog sort is likewise a service-defined query value.
"""

PRODUCT_URL = "https://github.com/qixuancao/pcbdraft"
NOUS_REGISTERED_OAUTH_CLIENT_ID = "hermes-cli"
DEEPINFRA_CATALOG_QUERY = "filter=true&sort_by=hermes"
