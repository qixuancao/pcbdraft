"""Stub for the optional ``nemo_relay`` dependency (Nous relay path, unused).

The relay runtime only imports this when the Nous relay feature is active,
which PCBDraft never enables.  Providing a stub keeps the vendored Hermes
tree import-compatible without pulling the real package.
"""

__version__ = "0.0.0"


def _none(*_args, **_kwargs):
    return None


# Common symbols the relay runtime probes for when it cannot load the real
# package.
class _Relay:
    def __getattr__(self, name):
        return _none


_relay = _Relay()


def __getattr__(name: str):
    return _relay