"""Validated, credential-reference-only provider configuration."""
from __future__ import annotations

import copy
from typing import Any

from contracts.security import SensitiveDataError, assert_no_sensitive_fields
from contracts.validator import accepted, validate


class ProviderConfigError(ValueError):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def validated_provider_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated validated copy; config may name but never contain credentials."""
    value = copy.deepcopy(config)
    try:
        assert_no_sensitive_fields(value, "provider_config")
    except SensitiveDataError as error:
        raise ProviderConfigError(str(error)) from error
    diagnostics = validate("provider_config", value)
    if not accepted(diagnostics):
        raise ProviderConfigError("invalid provider configuration", diagnostics)
    return value
