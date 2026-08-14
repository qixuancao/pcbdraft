"""PCBDraft runtime package and stable public identity."""

__version__ = "1.0.0"
PRODUCT_NAME = "PCBDraft"
DISTRIBUTION_NAME = "pcbdraft"
PRIMARY_CLI = "pcbdraft"

# Release 1.0 exposed most implementation modules directly below ``pcbdraft``.
# The implementation now lives in responsibility-focused subpackages, while a
# lazy import hook keeps those historical imports working for downstream users.
from ._compat import install_moved_module_aliases as _install_moved_module_aliases

_install_moved_module_aliases()
del _install_moved_module_aliases
