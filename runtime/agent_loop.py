"""Deterministic control for exact-one and continuous Agent loop protocols."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from agents.errors import AgentLoopError
from contracts.validator import accepted, validate
from infrastructure.persistence.repair_records import map_provider_stop
from runtime.worker_state import (
    RECOVERY_NOT_EXECUTED, RECOVERY_SUCCEEDED, WorkerStatePersistence,
    validate_recovery_observation,
)
from scripts.dvlib import validate_schema


class Transcript(Protocol):
    """Persistence interface required by :class:`AgentLoop`."""

    manifest: dict[str, Any] | None
    job_id: str
    role: str
    session_id: str
    entries: list[dict[str, Any]]

    def value(self, index: int, kind: str) -> Any | None: ...

    def record(self, kind: str, value: Any) -> None: ...

    def finalize(
            self, status: str, code: str,
            result_sequence: int | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentLoopPolicy:
    """Role-specific tool permissions and finite Worker budgets.

    ``retrieval_tools``/``submission_tools`` retain the original exact-one
    protocol.  The three newer sets opt a session into the continuous Worker
    protocol, where observations and actions may be repeated and only an
    accepted terminal result completes the loop.
    """

    role: str
    retrieval_tools: frozenset[str] = frozenset()
    submission_tools: frozenset[str] = frozenset()
    max_retrieval_turns: int = 3
    observation_tools: frozenset[str] = frozenset()
    action_tools: frozenset[str] = frozenset()
    terminal_tools: frozenset[str] = frozenset()
    max_turns: int | None = None
    max_time_seconds: float | None = None
    max_tokens: int | None = None
    max_actions: int | None = None

    def __post_init__(self) -> None:
        groups = (
            self.retrieval_tools, self.submission_tools,
            self.observation_tools, self.action_tools, self.terminal_tools,
        )
        all_names = [name for group in groups for name in group]
        continuous = bool(
            self.observation_tools or self.action_tools or self.terminal_tools)
        if (not self.role or len(all_names) != len(set(all_names)) or
                not 0 <= self.max_retrieval_turns <= 3 or
                (continuous and (self.retrieval_tools or self.submission_tools)) or
                (continuous and not self.terminal_tools) or
                (not continuous and not self.submission_tools) or
                any(not isinstance(name, str) or not name
                    for name in all_names) or
                any(value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or
                    value < 1)
                    for value in (
                        self.max_turns, self.max_tokens, self.max_actions)) or
                (self.max_time_seconds is not None and (
                    isinstance(self.max_time_seconds, bool) or
                    not isinstance(self.max_time_seconds, (int, float)) or
                    self.max_time_seconds <= 0))):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop policy is invalid")

    @property
    def continuous(self) -> bool:
        return bool(
            self.observation_tools or self.action_tools or self.terminal_tools)


class AgentLoop:
    """Run either a legacy exact-one session or a continuous Worker session.

    Continuous action handlers receive a deterministic ``action_id`` in their
    context.  A terminal request completes only when its validator decision is
    an object whose ``status`` is ``PASS``.
    """

    def __init__(
            self, *, provider: Any | None, transcript_store: Transcript,
            job_id: str, session_id: str,
            initial_messages: list[dict[str, str]],
            tools: list[dict[str, Any]],
            provider_binding: Mapping[str, str], policy: AgentLoopPolicy,
            retrieval_handlers: Mapping[
                str, Callable[[dict[str, Any]], Any]] | None = None,
            submission_handlers: Mapping[
                str, Callable[[dict[str, Any], dict[str, Any]], Any]] |
                None = None,
            observation_handlers: Mapping[
                str, Callable[[dict[str, Any]], Any]] | None = None,
            action_handlers: Mapping[
                str, Callable[[dict[str, Any], dict[str, Any]], Any]] |
                None = None,
            terminal_handlers: Mapping[
                str, Callable[[dict[str, Any], dict[str, Any]], Any]] |
                None = None,
            completion_validator: Callable[
                [dict[str, Any], dict[str, Any]], Any] | None = None,
            worker_state_store: WorkerStatePersistence | None = None,
            action_recovery_handlers: Mapping[
                str, Callable[[str, dict[str, Any], dict[str, Any]], Any]] |
                None = None,
            request_metadata: Mapping[str, Any] | None = None,
            cancel_requested: Callable[[], bool] | None = None,
            single_turn_request: Mapping[str, Any] | None = None,
            provider_request_id: str | None = None,
            provider_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
            clock: Callable[[], float] | None = None):
        if not initial_messages:
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop requires initial messages")
        if (transcript_store.job_id != job_id or
                transcript_store.role != policy.role or
                transcript_store.session_id != session_id):
            raise AgentLoopError(
                "STALE_EVIDENCE",
                "transcript store authority differs from the Agent loop")
        legacy_retrievals = dict(retrieval_handlers or {})
        legacy_submissions = dict(submission_handlers or {})
        observations = dict(observation_handlers or {})
        actions = dict(action_handlers or {})
        terminals = dict(terminal_handlers or {})
        names = [item.get("name") for item in tools]
        retrieval_names = set(legacy_retrievals)
        submission_names = set(legacy_submissions)
        observation_names = set(observations)
        action_names = set(actions)
        terminal_names = set(terminals)
        registered_groups = (
            retrieval_names, submission_names, observation_names,
            action_names, terminal_names,
        )
        registered = set().union(*registered_groups)
        if (retrieval_names != set(policy.retrieval_tools) or
                submission_names != set(policy.submission_tools) or
                observation_names != set(policy.observation_tools) or
                action_names != set(policy.action_tools) or
                terminal_names != set(policy.terminal_tools) or
                sum(len(group) for group in registered_groups) !=
                    len(registered) or
                any(not isinstance(name, str) or not name for name in names) or
                len(names) != len(set(names)) or
                not registered.issubset(set(names))):
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
        if completion_validator is not None and not policy.continuous:
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "completion validator requires the continuous Worker protocol")
        recovery_handlers = dict(action_recovery_handlers or {})
        if (worker_state_store is None and recovery_handlers) or (
                worker_state_store is not None and (
                    not policy.continuous or
                    worker_state_store.job_id != job_id or
                    worker_state_store.worker_session_id != session_id or
                    not set(recovery_handlers).issubset(action_names))):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "DV Worker state binding is invalid")
        if worker_state_store is not None:
            worker_state_store.verify_transcript_length(
                len(transcript_store.entries))
            if worker_state_store.current["status"] not in {
                    "RUNNING", "SUCCEEDED"}:
                raise AgentLoopError(
                    worker_state_store.current["status"],
                    "DV Worker must be explicitly resumed before execution")
        self.provider_call = (
            provider_call if provider_call is not None else
            provider.select_tools)
        self.transcript_store = transcript_store
        self.job_id = job_id
        self.role = policy.role
        self.session_id = session_id
        self.initial_messages = copy.deepcopy(initial_messages)
        allowed = registered
        self.tools = [copy.deepcopy(item) for item in tools
                      if item["name"] in allowed]
        self.retrieval_handlers = legacy_retrievals
        self.submission_handlers = legacy_submissions
        self.observation_handlers = observations
        self.action_handlers = actions
        self.terminal_handlers = terminals
        self.completion_validator = completion_validator
        self.worker_state_store = worker_state_store
        self.action_recovery_handlers = recovery_handlers
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
            if (self.single_turn_request.get("messages") !=
                        self.initial_messages or
                    self.single_turn_request.get("tools") != self.tools or
                    sorted(self.single_turn_request.get(
                        "legal_tool_names", [])) != sorted(allowed)):
                raise AgentLoopError(
                    "INVALID_TOOL_CALL",
                    "single-turn Agent request does not match its policy")
        if provider_request_id is not None and (
                not isinstance(provider_request_id, str) or
                not provider_request_id):
            raise AgentLoopError(
                "INVALID_TOOL_CALL", "Agent loop Provider request identity is invalid")
        self.provider_request_id = provider_request_id
        self.retrieval_count = 0
        self.turn_count = 0
        self.tokens_used = 0
        self.action_count = 0
        self.clock = clock or time.monotonic
        self._started_at: float | None = None
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
            if turn == 1:
                return copy.deepcopy(self.single_turn_request)
        legal = sorted(
            list(self.retrieval_handlers) + list(self.submission_handlers) +
            list(self.observation_handlers) + list(self.action_handlers) +
            list(self.terminal_handlers))
        return {
            "schema_version": "1.0",
            "request_id": self.provider_request_id or \
            "{}.TURN.{:03d}".format(self.session_id, turn),
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

    @staticmethod
    def _token_usage(response: Mapping[str, Any]) -> int:
        usage = response.get("usage", {})
        if not isinstance(usage, Mapping):
            return 0
        values = (usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values):
            raise AgentLoopError(
                "MALFORMED_MODEL_OUTPUT", "Provider token usage is invalid")
        return sum(values)

    @staticmethod
    def _completion_accepted(result: Any) -> bool:
        if not isinstance(result, Mapping):
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "continuous terminal validator must return a status decision")
        status = result.get("status")
        if not isinstance(status, str) or not status:
            raise AgentLoopError(
                "INVALID_TOOL_CALL",
                "continuous terminal validator must return a status decision")
        return status.upper() == "PASS"

    def _pause_if_budget_exhausted(self, *, before_tool: bool = False) -> None:
        policy = self.policy
        elapsed = (
            self.clock() - self._started_at
            if self._started_at is not None else 0.0)
        if before_tool:
            exhausted = (
                (policy.max_tokens is not None and
                 self.tokens_used > policy.max_tokens) or
                (policy.max_time_seconds is not None and
                 elapsed >= policy.max_time_seconds))
        else:
            exhausted = (
                (policy.max_turns is not None and
                 self.turn_count >= policy.max_turns) or
                (policy.max_tokens is not None and
                 self.tokens_used >= policy.max_tokens) or
                (policy.max_time_seconds is not None and
                 elapsed >= policy.max_time_seconds))
        if exhausted:
            raise AgentLoopError(
                "PAUSED_BUDGET", "Agent loop budget is exhausted")

    def _continuous_stop(
            self, response: Mapping[str, Any], calls: list[Mapping[str, Any]]) -> str:
        reason = str(response.get("finish_reason", "")).upper()
        if reason in {"CONTENT_FILTER", "CONTENT_FILTERED"}:
            return "CONTENT_FILTERED"
        if reason in {"REFUSAL", "MODEL_REFUSAL"}:
            return "MODEL_REFUSAL"
        if reason in {"LENGTH", "MAX_TOKENS"}:
            return "OUTPUT_LIMIT_EXCEEDED"
        if reason == "ERROR":
            diagnostic = next((
                item.get("code") for item in response.get("diagnostics", [])
                if isinstance(item, dict) and item.get("code")), None)
            return map_provider_stop(exception_code=diagnostic or "ERROR")
        if reason != "TOOL_CALLS" or len(calls) != 1:
            return (
                "TOOL_PROTOCOL_VIOLATION" if len(calls) > 1 else
                "MALFORMED_MODEL_OUTPUT")
        call = calls[0]
        if not isinstance(call.get("arguments"), dict):
            return "MALFORMED_MODEL_OUTPUT"
        if call.get("name") not in (
                set(self.observation_handlers) | set(self.action_handlers) |
                set(self.terminal_handlers)):
            return "TOOL_PROTOCOL_VIOLATION"
        return "TOOL_RESULT_REQUIRED"

    def _record_state_progress(
            self, cursor: int, phase: str | None = None) -> None:
        if self.worker_state_store is not None:
            current = self.worker_state_store.current
            if (cursor < current["transcript_cursor"] or
                    self.turn_count < current["turns_used"] or
                    self.tokens_used < current["tokens_used"]):
                return
            self.worker_state_store.record_progress(
                transcript_cursor=cursor, current_phase=phase,
                turns_used=self.turn_count, tokens_used=self.tokens_used)

    def _recover_pending_action(
            self, *, name: str, action_id: str,
            arguments: dict[str, Any], context: dict[str, Any], cursor: int
            ) -> tuple[bool, Any | None]:
        store = self.worker_state_store
        if store is None:
            return False, None
        handler = self.action_recovery_handlers.get(name)
        try:
            observation = (
                handler(action_id, copy.deepcopy(arguments),
                        copy.deepcopy(context))
                if handler is not None else {"status": "UNKNOWN"})
            status, result = validate_recovery_observation(observation)
        except Exception as error:
            status, result = "UNKNOWN", None
            message = "action recovery evidence is invalid: {}".format(
                type(error).__name__)
        else:
            message = "action side effect cannot be determined from evidence"
        if status == RECOVERY_SUCCEEDED:
            store.record_action_receipt(
                action_id=action_id, tool_name=name, arguments=arguments,
                result=result, transcript_cursor=cursor)
            return False, result
        if status == RECOVERY_NOT_EXECUTED:
            return True, None
        store.mark_status(
            "PAUSED_RECOVERY_REQUIRED", transcript_cursor=cursor,
            current_phase="ACT", error={
                "code": "PAUSED_RECOVERY_REQUIRED", "message": message,
                "action_id": action_id,
            })
        raise AgentLoopError("PAUSED_RECOVERY_REQUIRED", message)

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
        self._started_at = self.clock()
        final_status: str | None = None
        final_code = "SESSION_FAILED"
        result_sequence = None
        try:
            while True:
                if self.policy.continuous:
                    self._pause_if_budget_exhausted()
                turn += 1
                self.turn_count = turn
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
                self.tokens_used += self._token_usage(response)
                cursor += 1
                if self.policy.continuous:
                    self._record_state_progress(cursor, "OBSERVE")
                calls = response.get("tool_calls", [])
                if self.policy.continuous:
                    stop = self._continuous_stop(response, calls)
                else:
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
                name, arguments = call.get("name"), call.get("arguments")
                if name in self.action_handlers:
                    if (self.policy.max_actions is not None and
                            self.action_count >= self.policy.max_actions):
                        raise AgentLoopError(
                            "PAUSED_BUDGET",
                            "Agent loop action budget is exhausted")
                    self.action_count += 1
                    call["action_id"] = "{}.ACTION.{:03d}".format(
                        self.session_id, self.action_count)
                if self.policy.continuous:
                    self._pause_if_budget_exhausted(before_tool=True)
                persisted_call = transcript.value(cursor, "TOOL_CALL")
                if persisted_call is None:
                    transcript.record("TOOL_CALL", call)
                elif persisted_call != call:
                    raise AgentLoopError(
                        "STALE_EVIDENCE", "persisted tool call is stale")
                cursor += 1
                if (self.policy.continuous and
                        name not in self.action_handlers):
                    self._record_state_progress(
                        cursor,
                        "VALIDATE" if name in self.terminal_handlers else
                        "OBSERVE")
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

                context = {
                    "request": copy.deepcopy(request),
                    "response": copy.deepcopy(response),
                    "tool_name": name,
                    "session_id": self.session_id,
                    "turn": self.turn_count,
                    "tokens_used": self.tokens_used,
                    "actions_used": self.action_count,
                }
                if name in self.action_handlers:
                    context["action_id"] = call["action_id"]

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
                if name in self.terminal_handlers:
                    result = persisted_result
                    if result is None:
                        result = (
                            self.worker_state_store.terminal_decision(
                                str(call.get("call_id", "")))
                            if self.worker_state_store is not None else None)
                    if result is None:
                        terminal_result = self.terminal_handlers[str(name)](
                            copy.deepcopy(arguments), copy.deepcopy(context))
                        if self.completion_validator is None:
                            result = terminal_result
                        else:
                            validation_context = copy.deepcopy(context)
                            validation_context["terminal_result"] = copy.deepcopy(
                                terminal_result)
                            result = self.completion_validator(
                                copy.deepcopy(arguments), validation_context)
                        if self.worker_state_store is not None:
                            self.worker_state_store.record_terminal_decision(
                                tool_name=str(name),
                                call_id=str(call.get("call_id", "")),
                                result=result, transcript_cursor=cursor)
                    if persisted_result is None:
                        transcript.record("TOOL_RESULT", result)
                    cursor += 1
                    terminal_status = str(result.get("status", "")).upper() \
                        if isinstance(result, Mapping) else ""
                    if self._completion_accepted(result):
                        if self.worker_state_store is not None:
                            self.worker_state_store.mark_status(
                                "SUCCEEDED", transcript_cursor=cursor,
                                current_phase="COMPLETE")
                        final_status, final_code = "COMPLETED", "COMPLETED"
                        result_sequence = cursor
                        return result
                    if terminal_status in {
                            "PAUSED_BUDGET", "PAUSED_RETRYABLE",
                            "PAUSED_RECOVERY_REQUIRED", "BLOCKED_INPUT",
                            "BLOCKED_TOOL", "CANCELLED", "FAILED_POLICY",
                            "FAILED_INTERNAL"} and persisted_result is None:
                        if self.worker_state_store is not None:
                            self.worker_state_store.mark_status(
                                terminal_status, transcript_cursor=cursor,
                                current_phase=(
                                    "OBSERVE" if terminal_status.startswith(
                                        "PAUSED_") else "COMPLETE"),
                                error={
                                    "code": terminal_status,
                                    "message": str(result.get(
                                        "message", terminal_status)),
                                })
                        raise AgentLoopError(
                            terminal_status,
                            str(result.get("message", terminal_status)))
                    self._record_state_progress(cursor, "OBSERVE")
                    messages.append(self._history_message(
                        "MODEL_RESPONSE", response, "ASSISTANT"))
                    messages.append(self._history_message(
                        "TOOL_RESULT", {
                            "call_id": call.get("call_id", ""),
                            "tool_name": name, "result": result,
                        }, "USER"))
                    continue
                if name in self.action_handlers:
                    result = persisted_result
                    if result is None:
                        execute = True
                        if self.worker_state_store is not None:
                            action_id = str(call["action_id"])
                            action = self.worker_state_store.action_record(
                                action_id)
                            if action is None:
                                self.worker_state_store.record_action_intent(
                                    action_id=action_id, tool_name=str(name),
                                    arguments=arguments,
                                    transcript_cursor=cursor)
                            elif action["status"] == "SUCCEEDED":
                                result = copy.deepcopy(action["result"])
                                execute = False
                            else:
                                execute, result = self._recover_pending_action(
                                    name=str(name), action_id=action_id,
                                    arguments=arguments, context=context,
                                    cursor=cursor)
                        if execute:
                            result = self.action_handlers[str(name)](
                                copy.deepcopy(arguments), copy.deepcopy(context))
                            if self.worker_state_store is not None:
                                self.worker_state_store.record_action_receipt(
                                    action_id=str(call["action_id"]),
                                    tool_name=str(name), arguments=arguments,
                                    result=result, transcript_cursor=cursor)
                        transcript.record("TOOL_RESULT", result)
                    elif self.worker_state_store is not None:
                        action_id = str(call["action_id"])
                        action = self.worker_state_store.action_record(action_id)
                        if action is None:
                            self.worker_state_store.record_action_intent(
                                action_id=action_id, tool_name=str(name),
                                arguments=arguments,
                                transcript_cursor=cursor)
                            action = self.worker_state_store.action_record(action_id)
                        if action is not None and action["status"] == "INTENT":
                            self.worker_state_store.record_action_receipt(
                                action_id=action_id, tool_name=str(name),
                                arguments=arguments, result=result,
                                transcript_cursor=cursor)
                    cursor += 1
                    if self.worker_state_store is not None:
                        self.worker_state_store.record_observation(
                            tool_name=str(name),
                            call_id=str(call.get("call_id", "")),
                            result=result, transcript_cursor=cursor)
                    messages.append(self._history_message(
                        "MODEL_RESPONSE", response, "ASSISTANT"))
                    messages.append(self._history_message(
                        "TOOL_RESULT", {
                            "call_id": call.get("call_id", ""),
                            "tool_name": name,
                            "action_id": call["action_id"], "result": result,
                        }, "USER"))
                    continue
                if name in self.observation_handlers:
                    result = persisted_result
                    if result is None:
                        result = self.observation_handlers[str(name)](
                            copy.deepcopy(arguments))
                        transcript.record("TOOL_RESULT", result)
                    cursor += 1
                    if self.worker_state_store is not None:
                        self.worker_state_store.record_observation(
                            tool_name=str(name),
                            call_id=str(call.get("call_id", "")),
                            result=result, transcript_cursor=cursor)
                    messages.append(self._history_message(
                        "MODEL_RESPONSE", response, "ASSISTANT"))
                    messages.append(self._history_message(
                        "TOOL_RESULT", {
                            "call_id": call.get("call_id", ""),
                            "tool_name": name, "result": result,
                        }, "USER"))
                    continue
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
            if caught_code in {
                    "PAUSED_BUDGET", "PAUSED_RETRYABLE",
                    "PAUSED_RECOVERY_REQUIRED"}:
                final_status = None
            else:
                final_status = (
                    "CANCELLED" if caught_code == "CANCELLED" else "FAILED")
            if self.worker_state_store is not None:
                if caught_code == "PAUSED_BUDGET":
                    self.worker_state_store.mark_status(
                        "PAUSED_BUDGET", transcript_cursor=cursor,
                        current_phase="OBSERVE", error={
                            "code": "PAUSED_BUDGET",
                            "message": "Agent loop budget is exhausted",
                        })
                elif caught_code == "PAUSED_RETRYABLE":
                    self.worker_state_store.mark_status(
                        "PAUSED_RETRYABLE", transcript_cursor=cursor,
                        current_phase="OBSERVE", error={
                            "code": "PAUSED_RETRYABLE",
                            "message": str(caught),
                        })
                elif caught_code == "PAUSED_RECOVERY_REQUIRED":
                    # _recover_pending_action already persisted the exact
                    # action identity and uncertainty evidence.
                    pass
                elif caught_code == "CANCELLED":
                    self.worker_state_store.mark_status(
                        "CANCELLED", transcript_cursor=cursor,
                        current_phase="COMPLETE", error={
                            "code": "CANCELLED", "message": str(caught),
                        })
                elif caught_code in {
                        "BLOCKED_INPUT", "BLOCKED_TOOL", "FAILED_POLICY",
                        "FAILED_INTERNAL"}:
                    self.worker_state_store.mark_status(
                        caught_code, transcript_cursor=cursor,
                        current_phase="COMPLETE", error={
                            "code": caught_code, "message": str(caught),
                        })
                else:
                    policy_failure = caught_code in {
                        "MALFORMED_MODEL_OUTPUT", "TOOL_PROTOCOL_VIOLATION",
                        "TOOL_PERMISSION_DENIED", "INVALID_TOOL_CALL",
                        "INVALID_AGENT_BINDING", "INVALID_PROVIDER_REQUEST",
                        "CONTENT_FILTERED", "MODEL_REFUSAL",
                        "OUTPUT_LIMIT_EXCEEDED", "STALE_EVIDENCE",
                    }
                    self.worker_state_store.mark_status(
                        "FAILED_POLICY" if policy_failure else
                        "FAILED_INTERNAL",
                        transcript_cursor=cursor, current_phase="COMPLETE",
                        error={
                            "code": caught_code, "message": str(caught),
                        })
            final_code = caught_code
            raise
        finally:
            if final_status is not None:
                transcript.finalize(final_status, final_code, result_sequence)


__all__ = ["AgentLoop", "AgentLoopPolicy", "Transcript"]
