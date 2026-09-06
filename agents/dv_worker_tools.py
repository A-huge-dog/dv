"""Scoped tool contracts for the continuous UVM Generation Worker."""
from __future__ import annotations

import copy
from typing import Any, Mapping


GET_UVM_TASK_STATE = "get_uvm_task_state"
READ_UVM_CANDIDATE = "read_uvm_candidate"
WRITE_UVM_REPLACEMENTS = "write_uvm_replacements"
RUN_XCELIUM_COMPILE = "run_xcelium_compile"
READ_XCELIUM_OBSERVATION = "read_xcelium_observation"
FINISH_TASK = "finish_task"
PAUSE_TASK = "pause_task"
REPORT_BLOCKED = "report_blocked"

OBSERVATION_TOOLS = frozenset({
    GET_UVM_TASK_STATE,
    READ_UVM_CANDIDATE,
    READ_XCELIUM_OBSERVATION,
})
ACTION_TOOLS = frozenset({
    WRITE_UVM_REPLACEMENTS,
    RUN_XCELIUM_COMPILE,
})
TERMINAL_TOOLS = frozenset({
    FINISH_TASK,
    PAUSE_TASK,
    REPORT_BLOCKED,
})

_EMPTY = {
    "type": "object", "additionalProperties": False,
    "required": [], "properties": {},
}
_REPLACEMENTS = {
    "type": "object", "additionalProperties": False,
    "required": ["replacements"],
    "properties": {
        "replacements": {
            "type": "array", "minItems": 1, "maxItems": 128,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["logical_path", "content"],
                "properties": {
                    "logical_path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
    },
}


def uvm_worker_tool_definitions() -> list[dict[str, Any]]:
    """Return the complete fixed allow-list; no arbitrary path or shell tool."""
    descriptions = {
        GET_UVM_TASK_STATE:
            "Read the current UVM task authority, phase, and bounded budgets.",
        READ_UVM_CANDIDATE:
            "Read the complete current candidate from authorized UVM slots.",
        WRITE_UVM_REPLACEMENTS:
            "Replace every authorized generated UVM slot with exact content.",
        RUN_XCELIUM_COMPILE:
            "Compile and elaborate the current fingerprint-bound UVM candidate "
            "with the Framework-owned Xcelium harness.",
        READ_XCELIUM_OBSERVATION:
            "Read structured diagnostics and bounded excerpts from the latest run.",
        FINISH_TASK:
            "Request completion; the Framework reloads and validates all evidence.",
        PAUSE_TASK:
            "Request a retryable pause while retaining the current Worker state.",
        REPORT_BLOCKED:
            "Report a typed input or tool blocker that prevents further progress.",
    }
    schemas: dict[str, Mapping[str, Any]] = {
        GET_UVM_TASK_STATE: _EMPTY,
        READ_UVM_CANDIDATE: _EMPTY,
        WRITE_UVM_REPLACEMENTS: _REPLACEMENTS,
        RUN_XCELIUM_COMPILE: _EMPTY,
        READ_XCELIUM_OBSERVATION: _EMPTY,
        FINISH_TASK: _EMPTY,
        PAUSE_TASK: {
            "type": "object", "additionalProperties": False,
            "required": ["reason"],
            "properties": {"reason": {"type": "string", "minLength": 1,
                                        "maxLength": 1024}},
        },
        REPORT_BLOCKED: {
            "type": "object", "additionalProperties": False,
            "required": ["code", "reason"],
            "properties": {
                "code": {"enum": ["BLOCKED_INPUT", "BLOCKED_TOOL"]},
                "reason": {"type": "string", "minLength": 1,
                           "maxLength": 1024},
            },
        },
    }
    return [{
        "name": name,
        "description": descriptions[name],
        "input_schema": copy.deepcopy(dict(schemas[name])),
    } for name in (
        GET_UVM_TASK_STATE,
        READ_UVM_CANDIDATE,
        WRITE_UVM_REPLACEMENTS,
        RUN_XCELIUM_COMPILE,
        READ_XCELIUM_OBSERVATION,
        FINISH_TASK,
        PAUSE_TASK,
        REPORT_BLOCKED,
    )]


def bounded_xcelium_observation(
        result: Mapping[str, Any], *, excerpt_bytes: int = 4096
        ) -> dict[str, Any]:
    """Project exact logs into a bounded, structured model observation."""
    raw = result.get("raw_result", {})
    raw = raw if isinstance(raw, Mapping) else {}
    diagnostics = raw.get("diagnostic_codes", [])
    if not isinstance(diagnostics, list):
        diagnostics = []

    def excerpt(field: str) -> dict[str, Any]:
        text = str(result.get(field, ""))
        encoded = text.encode("utf-8")
        if len(encoded) <= excerpt_bytes:
            bounded = text
        else:
            bounded = encoded[:excerpt_bytes].decode("utf-8", errors="ignore")
        return {
            "text": bounded,
            "truncated": len(encoded) > excerpt_bytes,
            "total_bytes": len(encoded),
        }

    return {
        "status": result.get("status", "INCOMPLETE"),
        "exit_code": result.get("exit_code"),
        "request_fingerprint": result.get("request_fingerprint"),
        "result_fingerprint": result.get("result_fingerprint"),
        "diagnostics": sorted({str(item) for item in diagnostics}),
        "stdout_excerpt": excerpt("stdout"),
        "stderr_excerpt": excerpt("stderr"),
    }


__all__ = [
    "ACTION_TOOLS", "FINISH_TASK", "GET_UVM_TASK_STATE",
    "OBSERVATION_TOOLS", "PAUSE_TASK", "READ_UVM_CANDIDATE",
    "READ_XCELIUM_OBSERVATION", "REPORT_BLOCKED", "RUN_XCELIUM_COMPILE",
    "TERMINAL_TOOLS", "WRITE_UVM_REPLACEMENTS",
    "bounded_xcelium_observation", "uvm_worker_tool_definitions",
]
