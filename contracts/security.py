"""Shared rejection of credential-bearing structured data."""
from __future__ import annotations

from typing import Any


FORBIDDEN_KEYS = {
    "credential", "credentials", "password", "secret", "api_key", "private_key",
    "authorization", "access_token", "refresh_token", "auth_token",
}


class SensitiveDataError(ValueError):
    pass


def assert_no_sensitive_fields(value: Any, path: str = "value") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or normalized.endswith((
                    "_password", "_secret", "_api_key", "_private_key", "_credential")):
                raise SensitiveDataError("{}: credential field is not allowed".format(
                    path + "." + str(key)))
            assert_no_sensitive_fields(child, path + "." + str(key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_no_sensitive_fields(child, "{}[{}]".format(path, index))
