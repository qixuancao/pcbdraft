"""Focused tests for auxiliary provider failure classification."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.model import auxiliary_client as legacy
from pcbdraft.model import auxiliary_provider_failures as failures


class _HTTPError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code)


class AuxiliaryProviderFailureCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_classifiers_by_identity(self) -> None:
        names = (
            "_is_payment_error",
            "_is_rate_limit_error",
            "_is_timeout_error",
            "_is_connection_error",
            "_is_transient_transport_error",
            "_is_auth_error",
            "_is_unsupported_parameter_error",
            "_is_unsupported_temperature_error",
            "_is_model_not_found_error",
            "_is_model_incompatible_error",
            "_is_invalid_aux_response_error",
        )

        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(legacy, name), getattr(failures, name))

    def test_composed_classifiers_honor_legacy_monkeypatch_paths(self) -> None:
        with patch.object(legacy, "_is_connection_error", return_value=True):
            self.assertTrue(legacy._is_transient_transport_error(Exception("x")))
        with patch.object(
            legacy, "_is_unsupported_parameter_error", return_value=True
        ) as unsupported:
            self.assertTrue(legacy._is_unsupported_temperature_error(Exception("x")))
            unsupported.assert_called_once()
        with patch.object(legacy, "_is_model_not_found_error", return_value=True):
            self.assertFalse(
                legacy._is_model_incompatible_error(
                    _HTTPError("unsupported model", status_code=400)
                )
            )


class AuxiliaryProviderFailureBehaviorTests(unittest.TestCase):
    def test_payment_and_rate_limit_classification_do_not_overlap(self) -> None:
        billing = _HTTPError("insufficient credits", status_code=429)
        throttled = _HTTPError("too many requests; retry after 10s", status_code=429)

        self.assertTrue(failures._is_payment_error(billing))
        self.assertFalse(failures._is_rate_limit_error(billing))
        self.assertFalse(failures._is_payment_error(throttled))
        self.assertTrue(failures._is_rate_limit_error(throttled))
        self.assertTrue(
            failures._is_payment_error(_HTTPError("payment required", status_code=402))
        )

    def test_transport_classification_covers_timeout_connection_and_5xx(self) -> None:
        class ReadTimeout(Exception):
            pass

        self.assertTrue(failures._is_timeout_error(ReadTimeout("deadline")))
        self.assertTrue(failures._is_connection_error(Exception("connection reset")))
        self.assertTrue(
            failures._is_transient_transport_error(
                _HTTPError("upstream unavailable", status_code=503)
            )
        )
        self.assertFalse(
            failures._is_transient_transport_error(
                _HTTPError("bad request", status_code=400)
            )
        )

    def test_auth_and_unsupported_parameter_classification(self) -> None:
        self.assertTrue(failures._is_auth_error(_HTTPError("denied", status_code=401)))
        self.assertTrue(
            failures._is_auth_error(
                _HTTPError("unauthenticated:bad-credentials", status_code=403)
            )
        )
        self.assertTrue(
            failures._is_unsupported_parameter_error(
                _HTTPError("Unsupported parameter: max_tokens", status_code=400),
                "max_tokens",
            )
        )
        self.assertTrue(
            failures._is_unsupported_temperature_error(
                _HTTPError("temperature is not supported", status_code=400)
            )
        )

    def test_model_failure_categories_and_invalid_response(self) -> None:
        missing = _HTTPError("model_not_found", status_code=404)
        incompatible = _HTTPError(
            "model is not supported for this account", status_code=400
        )
        invalid = RuntimeError(
            "Auxiliary compression LLM returned invalid response: "
            "missing choices[0].message"
        )

        self.assertTrue(failures._is_model_not_found_error(missing))
        self.assertFalse(failures._is_model_incompatible_error(missing))
        self.assertTrue(failures._is_model_incompatible_error(incompatible))
        self.assertTrue(failures._is_invalid_aux_response_error(invalid))
        self.assertFalse(
            failures._is_invalid_aux_response_error(ValueError(str(invalid)))
        )


if __name__ == "__main__":
    unittest.main()
