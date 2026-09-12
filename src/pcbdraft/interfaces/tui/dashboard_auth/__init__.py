"""Dashboard authentication provider framework.

The dashboard auth gate engages only when the dashboard binds to a
non-loopback host without ``--insecure``. In that mode, every request must
carry a verified session from one of the registered ``DashboardAuthProvider``
plugins.

The Nous provider lives in ``plugins/dashboard-auth-nous/`` and is the
default. Third parties register their own providers via the plugin hook
``ctx.register_dashboard_auth_provider``.
"""

from pcbdraft.interfaces.tui.dashboard_auth.base import (
    DashboardAuthProvider,
    InvalidCodeError,
    InvalidCredentialsError,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
    TokenPrincipal,
    assert_protocol_compliance,
)
from pcbdraft.interfaces.tui.dashboard_auth.registry import (
    clear_providers,
    get_provider,
    list_providers,
    list_session_providers,
    list_token_providers,
    register_provider,
)

__all__ = [
    "DashboardAuthProvider",
    "InvalidCodeError",
    "InvalidCredentialsError",
    "LoginStart",
    "ProviderError",
    "RefreshExpiredError",
    "Session",
    "TokenPrincipal",
    "assert_protocol_compliance",
    "clear_providers",
    "get_provider",
    "list_providers",
    "list_session_providers",
    "list_token_providers",
    "register_provider",
]
