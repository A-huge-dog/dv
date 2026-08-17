"""Shared workflow stop-state classification."""
from __future__ import annotations

from typing import Any


NON_RECOVERABLE_CODES = frozenset({
    "ABANDONED_BY_HUMAN",
    "AUTHORITY_INPUT_LOST",
})

# These pauses require a Human/operator to restore the named authority before
# rerunning.  They are not terminal Job states.
RECOVERY_REQUIRED_CODES = frozenset({
    "CONFLICTING_REPLAY",
    "CROSS_JOB_ARTIFACT",
    "CROSS_JOB_EVIDENCE",
    "CROSS_SESSION_ARTIFACT",
    "INDEX_SUBSTITUTION",
    "INVALID_AGENT_BINDING",
    "INVALID_APPROVAL_PROVENANCE",
    "INVALID_RETRY_STATE",
    "PATH_ESCAPE",
    "SCOPE_EXPANSION",
    "STALE_DISPATCH",
    "STALE_EVIDENCE",
    "TAMPERED_ARTIFACT",
    "TOOL_PERMISSION_DENIED",
})


def stop_status(code: str) -> str:
    """Map one typed diagnostic to its public Job stop state."""
    if code in NON_RECOVERABLE_CODES:
        return "TERMINAL"
    if code in RECOVERY_REQUIRED_CODES or code.startswith(("CROSS_", "TAMPER")):
        return "PAUSED_RECOVERY_REQUIRED"
    return "PAUSED_RETRYABLE"


def stop_result(code: str, message: str, **context: Any) -> dict[str, Any]:
    """Build the common machine-readable stop payload."""
    return {
        "status": stop_status(code),
        "code": code,
        "message": message,
        **context,
    }


__all__ = [
    "NON_RECOVERABLE_CODES", "RECOVERY_REQUIRED_CODES", "stop_result",
    "stop_status",
]
