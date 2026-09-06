"""Typed Project workflow failures shared across runtime modules."""

from __future__ import annotations

import copy
from typing import Any


class ProjectJobError(ValueError):
    """Fail-closed Project error with a stable machine-readable code."""

    def __init__(
            self, code: str, message: str,
            failure_context: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.failure_context = copy.deepcopy(failure_context or {})
