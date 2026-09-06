"""Shared pure mapping invariants for Stage 1 and Stage 2."""
from __future__ import annotations

from typing import Any, Callable


def validate_semantic_completeness(
        completeness: dict[str, Any], omissions_key: str,
        error: Callable[..., Exception]) -> None:
    omissions = completeness[omissions_key]
    if completeness["declared_complete"] == bool(omissions):
        raise error(
            "FALSE_COMPLETENESS",
            "semantic completeness must be true with no omissions or false "
            "with one or more explicit omissions")
