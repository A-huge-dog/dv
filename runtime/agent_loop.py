"""Deterministic, resumable exact-one Agent loop protocol control."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from contracts.validator import accepted, validate
from core.project_oches003 import map_provider_stop
from scripts.dvlib import validate_schema


class AgentLoopError(RuntimeError):
    """Typed failure raised at the Agent protocol boundary."""

    def __init__(self, code: str, message: str):
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


class Transcript(Protocol):
    """Persistence interface required by :class:`AgentLoop`."""

    manifest: dict[str, Any] | None
    job_id: str
    role: str
    session_id: str

    def value(self, index: int, kind: str) -> Any | None: ...

    def record(self, kind: str, value: Any) -> None: ...

    def finalize(
            self, status: str, code: str,
            result_sequence: int | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentLoopPolicy:
    """Role-specific tool permission and retrieval budget."""

    role: str
    retrieval_tools: frozenset[str]
    submission_tools: frozenset[str]
    max_retrieval_turns: int = 3

    def __post_init__(self) -> None:
        if (not self.role or not self.submission_tools or
                self.submission_tools & self.retrieval_tools or
                not 0 <= self.max_retrieval_turns <= 3):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop policy is invalid")


class AgentLoop:
    """Run one request/tool/observation sequence to one final submission."""

    def __init__(
            self, *, provider: Any | None, transcript_store: Transcript,
            job_id: str, session_id: str,
            initial_messages: list[dict[str, str]],
            tools: list[dict[str, Any]],
            retrieval_handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
            submission_handlers: Mapping[
                str, Callable[[dict[str, Any], dict[str, Any]], Any]],
            provider_binding: Mapping[str, str], policy: AgentLoopPolicy,
            request_metadata: Mapping[str, Any] | None = None,
            cancel_requested: Callable[[], bool] | None = None,
            single_turn_request: Mapping[str, Any] | None = None,
            provider_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        if not initial_messages:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop requires initial messages")
        if (transcript_store.job_id != job_id or
                transcript_store.role != policy.role or
                transcript_store.session_id != session_id):
            raise AgentLoopError(
                "STALE_EVIDENCE",
                "transcript store authority differs from the Agent loop")
        names = [item.get("name") for item in tools]
        retrieval_names = set(retrieval_handlers)
        submission_names = set(submission_handlers)
        if (retrieval_names != set(policy.retrieval_tools) or
                submission_names != set(policy.submission_tools) or
                any(not isinstance(name, str) or not name for name in names) or
                len(names) != len(set(names)) or
                not (retrieval_names | submission_names).issubset(set(names))):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop tool registration is invalid")
        if (set(provider_binding) != {"provider_id", "model_id"} or
                any(not isinstance(value, str) or not value
                    for value in provider_binding.values())):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop provider binding is invalid")
        if (provider is None) == (provider_call is None):
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "Agent loop requires exactly one Provider invocation boundary")
        self.provider_call = (
            provider_call if provider_call is not None else
            provider.select_tools)
        self.transcript_store = transcript_store
        self.job_id = job_id
        self.role = policy.role
        self.session_id = session_id
        self.initial_messages = copy.deepcopy(initial_messages)
        allowed = retrieval_names | submission_names
        self.tools = [copy.deepcopy(item) for item in tools
                      if item["name"] in allowed]
        self.retrieval_handlers = dict(retrieval_handlers)
        self.submission_handlers = dict(submission_handlers)
        self.provider_binding = dict(provider_binding)
        self.policy = policy
        self.request_metadata = copy.deepcopy(dict(request_metadata or {}))
        if set(self.request_metadata) & {
                "job_id", "role", "session_id", "retrieval_turns_completed",
                "parallel_tool_calls"}:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop request metadata is reserved")
        self.cancel_requested = cancel_requested or (lambda: False)
        self.single_turn_request = (
            copy.deepcopy(dict(single_turn_request))
            if single_turn_request is not None else None)
        if self.single_turn_request is not None:
            if (policy.max_retrieval_turns != 0 or
                    self.single_turn_request.get("messages") !=
                        self.initial_messages or
                    self.single_turn_request.get("tools") != self.tools or
                    sorted(self.single_turn_request.get(
                        "legal_tool_names", [])) != sorted(allowed)):
                raise AgentLoopError(
                    "INVALID_TOOL_CALL",
                    "single-turn Agent request does not match its policy")
        self.retrieval_count = 0
        self._terminal = False

    @staticmethod
    def _history_message(label: str, value: Any, role: str) -> dict[str, str]:
        try:
            content = label + "\n" + json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "Agent loop history is not losslessly JSON serializable") \
                from error
        return {"role": role, "content": content}

    def _request(
            self, turn: int, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self.single_turn_request is not None:
            if turn != 1:
                raise AgentLoopError(
                    "TOOL_PROTOCOL_VIOLATION",
                    "single-turn Agent requested an additional Provider turn")
            return copy.deepcopy(self.single_turn_request)
        legal = sorted(list(self.retrieval_handlers) +
                       list(self.submission_handlers))
        return {
            "schema_version": "1.0",
            "request_id": "{}.TURN.{:03d}".format(self.session_id, turn),
            "operation": "SELECT_TOOLS",
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(self.tools),
            "tool_choice_policy": "REQUIRED", "legal_tool_names": legal,
            "metadata": {
                **copy.deepcopy(self.request_metadata),
                "job_id": self.job_id, "role": self.role,
                "session_id": self.session_id,
                "retrieval_turns_completed": self.retrieval_count,
                "parallel_tool_calls": False,
            },
        }

    def _validate_response(
            self, request: Mapping[str, Any], response: Mapping[str, Any]
            ) -> None:
        if not accepted(validate("provider_response", response)):
            raise AgentLoopError(
                "MALFORMED_MODEL_OUTPUT",
                "Provider response violates the response contract")
        if (response.get("request_id") != request["request_id"] or
                response.get("operation") != "SELECT_TOOLS" or
                response.get("model_id") != self.provider_binding["model_id"] or
                response.get("provider_metadata", {}).get("provider_id") !=
                    self.provider_binding["provider_id"]):
            raise AgentLoopError(
                "INVALID_AGENT_BINDING",
                "Provider response does not match the fixed Agent binding")

    def run(self) -> Any:
        if self._terminal:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop already reached a terminal result")
        self._terminal = True
        transcript = self.transcript_store
        if transcript.manifest is not None:
            terminal = transcript.manifest["terminal"]
            if terminal["status"] == "COMPLETED":
                sequence = terminal["result_sequence"]
                if sequence is None:
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "completed Agent loop has no result")
                return transcript.value(sequence - 1, "TOOL_RESULT")
            raise AgentLoopError(
                terminal["code"], "persisted Agent loop is terminal")

        messages = copy.deepcopy(self.initial_messages)
        used_names: set[str] = set()
        cursor = 0
        turn = 0
        final_status, final_code = "FAILED", "SESSION_FAILED"
        result_sequence = None
        try:
            while True:
                turn += 1
                expected = self._request(turn, messages)
                request = transcript.value(cursor, "REQUEST")
                if request is None:
                    request = expected
                    transcript.record("REQUEST", request)
                elif request != expected:
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "persisted Agent request is stale")
                cursor += 1

                response = transcript.value(cursor, "RESPONSE")
                if response is None:
                    try:
                        response = self.provider_call(request)
                    except Exception as provider_error:
                        if isinstance(provider_error, AgentLoopError):
                            raise
                        error_code = getattr(provider_error, "code", "")
                        error_message = getattr(
                            provider_error, "message", str(provider_error))
                        if isinstance(error_code, str) and error_code:
                            raise AgentLoopError(
                                error_code, str(error_message)) from provider_error
                        if getattr(provider_error, "code", "") == \
                                "INVALID_PROVIDER_REQUEST":
                            raise AgentLoopError(
                                "INVALID_PROVIDER_REQUEST",
                                "Provider request violates the local adapter "
                                "contract") from provider_error
                        raise AgentLoopError(
                            "PROVIDER_UNAVAILABLE",
                            "Provider failed after its configured retry policy") \
                            from provider_error
                    transcript.record("RESPONSE", response)
                self._validate_response(request, response)
                cursor += 1
                calls = response.get("tool_calls", [])
                stop = map_provider_stop(
                    finish_reason=response.get("finish_reason"),
                    exception_code=(next((
                        item.get("code") for item in response.get(
                            "diagnostics", [])
                        if isinstance(item, dict) and item.get("code")), None)
                        if response.get("finish_reason") == "ERROR" else None),
                    tool_calls=calls,
                    legal_tools=self.retrieval_handlers,
                    submission_tools=self.submission_handlers,
                    used_retrievals=used_names,
                    retrieval_count=self.retrieval_count,
                    arguments_valid=(len(calls) == 1 and isinstance(
                        calls[0].get("arguments"), dict)))
                if stop not in {"COMPLETED", "TOOL_RESULT_REQUIRED"}:
                    raise AgentLoopError(
                        stop, "Provider stopped without one legal next action")
                call = copy.deepcopy(calls[0])
                persisted_call = transcript.value(cursor, "TOOL_CALL")
                if persisted_call is None:
                    transcript.record("TOOL_CALL", call)
                elif persisted_call != call:
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "persisted tool call is stale")
                cursor += 1
                name, arguments = call.get("name"), call.get("arguments")
                if not isinstance(arguments, dict):
                    raise AgentLoopError(
                        "MALFORMED_MODEL_OUTPUT", "tool arguments must be an object")
                selected_tools = [item for item in self.tools
                                  if item["name"] == name]
                if (len(selected_tools) != 1 or validate_schema(
                        arguments, selected_tools[0]["input_schema"],
                        "tool_arguments")):
                    raise AgentLoopError(
                        "MALFORMED_MODEL_OUTPUT",
                        "tool arguments violate the selected tool schema")
                persisted_result = transcript.value(cursor, "TOOL_RESULT")
                if persisted_result is None and self.cancel_requested():
                    raise AgentLoopError(
                        "CANCELLED", "Agent loop was cancelled before tool execution")

                if name in self.submission_handlers:
                    result = persisted_result
                    if result is None:
                        result = self.submission_handlers[str(name)](
                            copy.deepcopy(arguments), {
                                "request": copy.deepcopy(request),
                                "response": copy.deepcopy(response),
                                "retrieval_rounds": self.retrieval_count,
                            })
                        transcript.record("TOOL_RESULT", result)
                    cursor += 1
                    final_status, final_code = "COMPLETED", "COMPLETED"
                    result_sequence = cursor
                    return result
                if name not in self.retrieval_handlers:
                    raise AgentLoopError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "tool is outside the role-specific retrieval allow-list")
                if name in used_names:
                    raise AgentLoopError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "a retrieval tool name may be used only once per Agent loop")
                if self.retrieval_count >= self.policy.max_retrieval_turns:
                    raise AgentLoopError(
                        "TOOL_PROTOCOL_VIOLATION",
                        "Agent loop requested a fourth retrieval turn")
                used_names.add(str(name))
                self.retrieval_count += 1
                result = persisted_result
                if result is None:
                    result = self.retrieval_handlers[str(name)](
                        copy.deepcopy(arguments))
                    transcript.record("TOOL_RESULT", result)
                cursor += 1
                messages.append(self._history_message(
                    "MODEL_RESPONSE", response, "ASSISTANT"))
                messages.append(self._history_message(
                    "TOOL_RESULT", {
                        "call_id": call.get("call_id", ""),
                        "tool_name": name, "result": result,
                    }, "USER"))
        except Exception as caught:
            caught_code = getattr(caught, "code", type(caught).__name__)
            final_status = "CANCELLED" if caught_code == "CANCELLED" else "FAILED"
            final_code = caught_code
            raise
        finally:
            transcript.finalize(final_status, final_code, result_sequence)


__all__ = ["AgentLoop", "AgentLoopError", "AgentLoopPolicy", "Transcript"]
