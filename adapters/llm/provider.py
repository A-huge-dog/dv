"""Provider-neutral, contract-validating LLM interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from contracts.security import SensitiveDataError, assert_no_sensitive_fields
from contracts.validator import accepted, validate


class ProviderContractError(ValueError):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _require_contract(kind: str, value: dict[str, Any]) -> None:
    try:
        assert_no_sensitive_fields(value, kind)
    except SensitiveDataError as error:
        raise ProviderContractError(str(error)) from error
    diagnostics = validate(kind, value)
    if not accepted(diagnostics):
        raise ProviderContractError("invalid {} contract".format(kind), diagnostics)


def _request_semantics(request: dict[str, Any], expected_operation: str) -> None:
    if request["operation"] != expected_operation:
        raise ProviderContractError("provider operation does not match API method")
    if not request["messages"]:
        raise ProviderContractError("provider request requires at least one message")
    tool_names = [tool["name"] for tool in request["tools"]]
    if len(tool_names) != len(set(tool_names)):
        raise ProviderContractError("provider request contains duplicate tool names")
    legal = request["legal_tool_names"]
    if len(legal) != len(set(legal)) or not set(legal).issubset(set(tool_names)):
        raise ProviderContractError("legal tool names must be unique registered request tools")
    if expected_operation == "SELECT_TOOLS":
        if request["tool_choice_policy"] not in ("REQUIRED", "CONSTRAINED") or not legal:
            raise ProviderContractError("tool selection requires a non-empty constrained set")
    if request["tool_choice_policy"] == "NONE" and (request["tools"] or legal):
        raise ProviderContractError("NONE tool policy cannot carry tools")


def _response_semantics(response: dict[str, Any], request: dict[str, Any]) -> None:
    if response["request_id"] != request["request_id"] or response["operation"] != request["operation"]:
        raise ProviderContractError("provider response does not match request identity")
    call_ids = [call["call_id"] for call in response["tool_calls"]]
    if len(call_ids) != len(set(call_ids)):
        raise ProviderContractError("provider response contains duplicate tool call IDs")
    legal = set(request["legal_tool_names"])
    if any(call["name"] not in legal for call in response["tool_calls"]):
        raise ProviderContractError("provider selected a tool outside the legal set")
    if response["tool_calls"] and response["finish_reason"] != "TOOL_CALLS":
        raise ProviderContractError("tool calls require TOOL_CALLS finish reason")
    if response["finish_reason"] == "TOOL_CALLS" and not response["tool_calls"]:
        raise ProviderContractError("TOOL_CALLS finish reason requires at least one call")
    for item in response["diagnostics"]:
        _require_contract("diagnostics", item)


class LLMProvider(ABC):
    """Validated public API; implementations supply only protected backend hooks."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    def probe(self) -> dict[str, Any]:
        response = self._probe()
        _require_contract("provider_probe", response)
        if response["provider_id"] != self.provider_id:
            raise ProviderContractError("provider probe identity mismatch")
        for item in response["diagnostics"]:
            _require_contract("diagnostics", item)
        if response["status"] == "PASS" and not response["tool_call_capable"]:
            raise ProviderContractError("passing provider probe must support tool calls")
        if response["status"] == "FAIL" and not response["diagnostics"]:
            raise ProviderContractError("failed provider probe requires diagnostics")
        return response

    def restore_probe(self, response: dict[str, Any]) -> None:
        """Restore process-local readiness from validated PASS evidence."""
        _require_contract("provider_probe", response)
        if response["provider_id"] != self.provider_id:
            raise ProviderContractError("persisted provider probe identity mismatch")
        if response["status"] != "PASS" or not response["tool_call_capable"]:
            raise ProviderContractError(
                "only a tool-capable PASS provider probe can be restored")
        self._restore_probe(response)

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("COMPLETE", request, self._complete)

    def select_tools(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("SELECT_TOOLS", request, self._select_tools)

    def _invoke(self, operation: str, request: dict[str, Any], backend) -> dict[str, Any]:
        _require_contract("provider_request", request)
        _request_semantics(request, operation)
        response = backend(request)
        _require_contract("provider_response", response)
        _response_semantics(response, request)
        return response

    @abstractmethod
    def _probe(self) -> dict[str, Any]:
        raise NotImplementedError

    def _restore_probe(self, response: dict[str, Any]) -> None:
        """Adapter hook for restoring process-local readiness state."""

    @abstractmethod
    def _complete(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def _select_tools(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
