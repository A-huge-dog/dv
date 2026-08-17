"""Configured provider for supported OpenAI-compatible API dialects."""
from __future__ import annotations

import copy
import json
import os
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

from contracts.validator import diagnostic

from .provider import LLMProvider, ProviderContractError
from .provider_config import validated_provider_config


PROBE_TOOL_NAME = "dv_agent_capability_probe"
SAFE_REMOTE_ATOM = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_remote_signature(error: Exception) -> str:
    """Return bounded routing metadata without remote text or response data."""
    def atom(value: Any) -> str:
        text = str(value) if value is not None else "unknown"
        return text if SAFE_REMOTE_ATOM.fullmatch(text) else "redacted"

    status = getattr(error, "status_code", None)
    status_text = str(status) if isinstance(status, int) and \
        100 <= status <= 599 else "unknown"
    return "type={} status={} code={} param={}".format(
        atom(type(error).__name__),
        status_text,
        atom(getattr(error, "code", None)),
        atom(getattr(error, "param", None)))


class OpenAICompatibleProvider(LLMProvider):
    """Translate provider-neutral contracts to a configured compatible API."""

    def __init__(self, config: dict[str, Any],
                 client_factory: Callable[..., Any] | None = None):
        self._config = validated_provider_config(config)
        self._client_factory = client_factory or self._default_client_factory
        self._probe_passed = False

    @staticmethod
    def _default_client_factory(**kwargs: Any) -> Any:
        from openai import OpenAI
        return OpenAI(**kwargs)

    @property
    def provider_id(self) -> str:
        return self._config["provider_id"]

    @property
    def model_id(self) -> str:
        """Expose the immutable configured model identity for role binding."""
        return self._config["model_id"]

    def __repr__(self) -> str:
        return "OpenAICompatibleProvider(provider_id={!r}, model_id={!r})".format(
            self.provider_id, self._config["model_id"])

    def _sdk_version(self) -> str:
        try:
            return version("openai")
        except PackageNotFoundError:
            return "unavailable"

    def _new_client(self) -> Any:
        auth_env = self._config["auth_env"]
        credential = os.environ.get(auth_env)
        if not credential:
            raise ProviderContractError(
                "configured provider credential reference is unavailable")
        return self._client_factory(
            api_key=credential,
            base_url=self._config["endpoint"],
            timeout=float(self._config["timeout_seconds"]),
            max_retries=self._config["max_retries"])

    def _probe_failure(
            self, code: str, message: str, artifact_kind: str,
            path: str = "provider_config") -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "provider_id": self.provider_id,
            "model_id": self._config["model_id"],
            "status": "FAIL",
            "tool_call_capable": False,
            "provider_version": self._sdk_version(),
            "diagnostics": [diagnostic(
                code, message, path, self.provider_id,
                required_owner="DV_AGENT_DEVELOPER",
                required_artifact_kind=artifact_kind)],
        }

    @staticmethod
    def _chat_probe_tool() -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": PROBE_TOOL_NAME,
                "description": "Confirm provider function-call capability.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }

    @staticmethod
    def _responses_probe_tool() -> dict[str, Any]:
        return {
            "type": "function",
            "name": PROBE_TOOL_NAME,
            "description": "Confirm provider function-call capability.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @staticmethod
    def _choice(response: Any) -> Any:
        choices = _read(response, "choices", []) or []
        if not choices:
            raise ProviderContractError(
                "configured provider returned no completion choice")
        return choices[0]

    @classmethod
    def _message(cls, response: Any) -> Any:
        message = _read(cls._choice(response), "message")
        if message is None:
            raise ProviderContractError(
                "configured provider returned no completion message")
        return message

    @staticmethod
    def _tool_calls(message: Any) -> list[Any]:
        return list(_read(message, "tool_calls", []) or [])

    def _reasoning_parameters(self) -> dict[str, Any]:
        effort = self._config.get("reasoning_effort")
        return {"reasoning": {"effort": effort}} if effort else {}

    def _create(self, client: Any, parameters: dict[str, Any]) -> Any:
        if self._config["api_version"] == "responses-v1":
            return client.responses.create(**parameters)
        return client.chat.completions.create(**parameters)

    def _probe_parameters(self, *, tool: bool) -> dict[str, Any]:
        token_limit = min(
            self._config["max_output_tokens"], 1024 if tool else 32)
        if self._config["api_version"] == "responses-v1":
            prompt = (
                "Call the dv_agent_capability_probe function now. "
                "Do not answer with text."
                if tool else "Reply with the single word OK.")
            parameters: dict[str, Any] = {
                "model": self._config["model_id"],
                "input": prompt,
                "stream": False,
                "store": self._config["store"],
                "max_output_tokens": token_limit,
                **self._reasoning_parameters(),
            }
            if tool:
                parameters.update({
                    "tools": [self._responses_probe_tool()],
                    "tool_choice": {
                        "type": "function",
                        "name": PROBE_TOOL_NAME,
                    },
                    "parallel_tool_calls": False,
                })
            return parameters
        parameters = {
            "model": self._config["model_id"],
            "messages": [{
                "role": "user",
                "content": (
                    "Call the dv_agent_capability_probe function now. "
                    "Do not answer with text."
                    if tool else "Reply with the single word OK."),
            }],
            "stream": False,
            "max_tokens": token_limit,
        }
        if "reasoning_effort" in self._config:
            parameters["reasoning_effort"] = \
                self._config["reasoning_effort"]
        if tool:
            parameters.update({
                "tools": [self._chat_probe_tool()],
                "tool_choice": "auto",
            })
        return parameters

    @staticmethod
    def _responses_tool_calls(response: Any) -> list[Any]:
        return [
            item for item in list(_read(response, "output", []) or [])
            if _read(item, "type") == "function_call"]

    def _probe(self) -> dict[str, Any]:
        self._probe_passed = False
        if not os.environ.get(self._config["auth_env"]):
            return self._probe_failure(
                "PROVIDER_CONFIG_MISSING",
                "Configured provider authentication environment reference is unavailable",
                "PROVIDER_CREDENTIAL_REFERENCE",
                "provider_config.auth_env")
        try:
            client = self._new_client()
        except Exception as error:
            return self._probe_failure(
                "PROVIDER_PROBE_FAILED",
                "Configured provider client initialization failed; {}".format(
                    _safe_remote_signature(error)),
                "LLM_PROVIDER_CONFIG",
                "provider_config.endpoint")
        try:
            self._create(client, self._probe_parameters(tool=False))
        except Exception as error:
            return self._probe_failure(
                "PROVIDER_PROBE_FAILED",
                "Configured model probe failed; {}".format(
                    _safe_remote_signature(error)),
                "LLM_PROVIDER_CONFIG",
                "provider_config.model_id")
        try:
            response = self._create(
                client, self._probe_parameters(tool=True))
            if self._config["api_version"] == "responses-v1":
                calls = self._responses_tool_calls(response)
                capable = any(
                    bool(_read(call, "call_id")) and
                    _read(call, "name") == PROBE_TOOL_NAME and
                    self._parse_arguments(
                        _read(call, "arguments", "{}")) == {}
                    for call in calls)
            else:
                calls = self._tool_calls(self._message(response))
                capable = any(
                    _read(call, "type") == "function" and
                    bool(_read(call, "id")) and
                    _read(_read(call, "function", {}), "name") ==
                    PROBE_TOOL_NAME and
                    self._parse_arguments(
                        _read(_read(call, "function", {}),
                              "arguments", "{}")) == {}
                    for call in calls)
            if not capable:
                return self._probe_failure(
                    "PROVIDER_PROBE_FAILED",
                    "Configured provider did not return the required function capability probe",
                    "LLM_PROVIDER_CONFIG",
                    "provider_config.tool_calls_required")
        except Exception as error:
            return self._probe_failure(
                "PROVIDER_PROBE_FAILED",
                "Configured function-call probe failed; {}".format(
                    _safe_remote_signature(error)),
                "LLM_PROVIDER_CONFIG",
                "provider_config.tool_calls_required")
        self._probe_passed = True
        return {
            "schema_version": "1.0",
            "provider_id": self.provider_id,
            "model_id": self._config["model_id"],
            "status": "PASS",
            "tool_call_capable": True,
            "provider_version": self._sdk_version(),
            "diagnostics": [],
        }

    def _restore_probe(self, response: dict[str, Any]) -> None:
        if response.get("model_id") != self._config["model_id"]:
            raise ProviderContractError(
                "persisted provider probe model identity mismatch")
        self._probe_passed = True

    @staticmethod
    def _parse_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            value = copy.deepcopy(raw)
        elif isinstance(raw, str):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise ProviderContractError(
                    "provider returned malformed tool arguments") from error
        else:
            raise ProviderContractError(
                "provider returned non-object tool arguments")
        if not isinstance(value, dict):
            raise ProviderContractError(
                "provider returned non-object tool arguments")
        return value

    @staticmethod
    def _compatible_tool_schema(
            value: Any, root: dict[str, Any] | None = None) -> Any:
        """Expand local schema references and retain the endpoint subset.

        The complete contract remains available to the Framework validator.
        Provider-only conversion intentionally drops locally enforced keywords
        such as ``uniqueItems`` instead of weakening the local contract.
        """
        if root is None and isinstance(value, dict):
            root = value
        if isinstance(value, list):
            return [
                OpenAICompatibleProvider._compatible_tool_schema(item, root)
                for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or \
                    not reference.startswith("#/") or root is None:
                raise ProviderContractError(
                    "configured provider tool schema has an unsupported reference")
            resolved: Any = root
            try:
                for token in reference[2:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    resolved = resolved[token]
            except (KeyError, TypeError):
                raise ProviderContractError(
                    "configured provider tool schema reference cannot be resolved")
            return OpenAICompatibleProvider._compatible_tool_schema(
                resolved, root)
        result = {}
        for key, item in value.items():
            if key == "properties":
                if isinstance(item, dict):
                    result[key] = {
                        property_name:
                            OpenAICompatibleProvider._compatible_tool_schema(
                                property_schema, root)
                        for property_name, property_schema in item.items()
                    }
            elif key in {
                    "type", "required", "items", "enum", "description",
                    "pattern", "minLength", "maxLength", "minimum",
                    "maximum", "minItems", "maxItems", "anyOf",
                    "additionalProperties"}:
                result[key] = \
                    OpenAICompatibleProvider._compatible_tool_schema(
                        item, root)
            elif key == "const":
                result["enum"] = [copy.deepcopy(item)]
        return result

    @staticmethod
    def _strict_schema_gate(value: Any, path: str = "parameters") -> None:
        """Reject schemas outside the recursively closed strict subset."""
        if not isinstance(value, dict):
            raise ProviderContractError(
                "configured provider strict tool schema at {} must be an "
                "object".format(path))
        allowed = {
            "type", "properties", "required", "additionalProperties",
            "items", "enum", "description", "pattern", "minLength",
            "maxLength", "minimum", "maximum", "minItems", "maxItems",
            "anyOf",
        }
        unsupported = sorted(set(value) - allowed)
        if unsupported:
            raise ProviderContractError(
                "configured provider strict tool schema at {} contains "
                "unsupported keywords: {}".format(
                    path, ", ".join(unsupported)))
        schema_type = value.get("type")
        if schema_type == "object":
            properties = value.get("properties")
            required = value.get("required")
            if (not isinstance(properties, dict) or
                    not isinstance(required, list) or
                    any(not isinstance(item, str) for item in required) or
                    len(required) != len(set(required)) or
                    set(required) != set(properties) or
                    value.get("additionalProperties") is not False):
                raise ProviderContractError(
                    "configured provider strict tool schema at {} requires "
                    "explicit object properties, every property required, "
                    "and additionalProperties false".format(path))
            for name, child in properties.items():
                if not isinstance(name, str):
                    raise ProviderContractError(
                        "configured provider strict tool schema at {} has a "
                        "non-string property name".format(path))
                OpenAICompatibleProvider._strict_schema_gate(
                    child, "{}.properties.{}".format(path, name))
        elif schema_type == "array":
            if "items" not in value:
                raise ProviderContractError(
                    "configured provider strict tool schema at {} requires "
                    "explicit array items".format(path))
            OpenAICompatibleProvider._strict_schema_gate(
                value["items"], "{}.items".format(path))
        elif schema_type not in {
                None, "string", "integer", "number", "boolean", "null"}:
            raise ProviderContractError(
                "configured provider strict tool schema at {} has unsupported "
                "type".format(path))
        if "properties" in value and schema_type != "object":
            raise ProviderContractError(
                "configured provider strict tool schema at {} places "
                "properties on a non-object".format(path))
        if any(key in value for key in ("required", "additionalProperties")) \
                and schema_type != "object":
            raise ProviderContractError(
                "configured provider strict tool schema at {} places object "
                "keywords on a non-object".format(path))
        if "items" in value and schema_type != "array":
            raise ProviderContractError(
                "configured provider strict tool schema at {} places items "
                "on a non-array".format(path))
        alternatives = value.get("anyOf")
        if alternatives is not None:
            if not isinstance(alternatives, list) or not alternatives:
                raise ProviderContractError(
                    "configured provider strict tool schema at {} has invalid "
                    "anyOf".format(path))
            for index, child in enumerate(alternatives):
                OpenAICompatibleProvider._strict_schema_gate(
                    child, "{}.anyOf[{}]".format(path, index))

    @staticmethod
    def _chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
        parameters = copy.deepcopy(tool["input_schema"])
        compatible = OpenAICompatibleProvider._compatible_tool_schema(
            parameters)
        OpenAICompatibleProvider._strict_schema_gate(compatible)
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": compatible,
                "strict": True,
            },
        }

    @staticmethod
    def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        chat_tool = OpenAICompatibleProvider._chat_tool(tool)["function"]
        return {
            "type": "function",
            "name": chat_tool["name"],
            "description": chat_tool["description"],
            "parameters": chat_tool["parameters"],
            "strict": True,
        }

    @staticmethod
    def _responses_messages(
            messages: list[dict[str, Any]],
            ) -> tuple[str | None, list[dict[str, Any]]]:
        """Translate neutral messages to canonical Responses input items."""
        instructions = []
        input_items = []
        for index, message in enumerate(messages):
            role = message["role"].lower()
            content = message["content"]
            if role == "system":
                instructions.append(content)
            elif role == "assistant":
                input_items.append({
                    "type": "message",
                    "id": "msg_history{:04d}".format(index),
                    "status": "completed",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                    }],
                })
            else:
                input_items.append({
                    "type": "message",
                    "role": role,
                    "content": [{
                        "type": "input_text",
                        "text": content,
                    }],
                })
        return (
            "\n\n".join(instructions) if instructions else None,
            input_items)

    def _request_parameters(
            self, request: dict[str, Any]) -> dict[str, Any]:
        messages = [{
            "role": message["role"].lower(),
            "content": message["content"],
        } for message in request["messages"] if message["role"] != "TOOL"]
        legal = set(request["legal_tool_names"])
        eligible_tools = [
            tool for tool in request["tools"] if tool["name"] in legal]
        if self._config["api_version"] == "responses-v1":
            tools = [self._responses_tool(tool) for tool in eligible_tools]
            instructions, input_items = self._responses_messages(
                request["messages"])
            parameters: dict[str, Any] = {
                "model": self._config["model_id"],
                "input": input_items,
                "stream": False,
                "store": self._config["store"],
                "max_output_tokens": self._config["max_output_tokens"],
                **self._reasoning_parameters(),
            }
            if instructions is not None:
                parameters["instructions"] = instructions
            if tools:
                parameters.update({
                    "tools": tools,
                    "tool_choice": (
                        "required"
                        if request["tool_choice_policy"] in {
                            "REQUIRED", "CONSTRAINED"}
                        else "auto"),
                    "parallel_tool_calls": False,
                })
            return parameters
        tools = [self._chat_tool(tool) for tool in eligible_tools]
        parameters: dict[str, Any] = {
            "model": self._config["model_id"],
            "messages": messages,
            "stream": False,
            "max_tokens": self._config["max_output_tokens"],
        }
        if "reasoning_effort" in self._config:
            parameters["reasoning_effort"] = \
                self._config["reasoning_effort"]
        if "enable_thinking" in self._config:
            parameters["extra_body"] = {
                "enable_thinking": self._config["enable_thinking"],
            }
        if tools:
            parameters["tools"] = tools
            # The compatible dialect uses its portable tool-choice subset.
            # Project callers enforce exact-one legal tool semantics before
            # accepting an artifact.
            parameters["tool_choice"] = "auto"
        return parameters

    def _chat_response_contract(
            self, request: dict[str, Any], response: Any) -> dict[str, Any]:
        choice = self._choice(response)
        message = _read(choice, "message")
        if message is None:
            raise ProviderContractError(
                "configured provider returned no completion message")
        calls = []
        for item in self._tool_calls(message):
            function = _read(item, "function", {})
            calls.append({
                "call_id": _read(item, "id", ""),
                "name": _read(function, "name", ""),
                "arguments": self._parse_arguments(
                    _read(function, "arguments", "{}")),
            })
        reason = _read(choice, "finish_reason", "stop")
        if calls or reason == "tool_calls":
            finish_reason = "TOOL_CALLS"
        elif reason == "length":
            finish_reason = "LENGTH"
        elif reason == "content_filter":
            finish_reason = "CONTENT_FILTER"
        elif reason in ("error", "cancelled"):
            finish_reason = "ERROR"
        else:
            finish_reason = "STOP"
        usage = _read(response, "usage", {}) or {}
        content = _read(message, "content", "") or ""
        if not isinstance(content, str):
            raise ProviderContractError(
                "configured provider returned non-text completion content")
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": request["operation"],
            "finish_reason": finish_reason,
            "content": content,
            "tool_calls": calls,
            "usage": {
                "input_tokens": _read(usage, "prompt_tokens", 0) or 0,
                "output_tokens": _read(usage, "completion_tokens", 0) or 0,
            },
            "model_id": _read(
                response, "model", self._config["model_id"]),
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": _read(response, "id", ""),
                "provider_status": reason,
            },
            "diagnostics": [],
        }

    def _responses_response_contract(
            self, request: dict[str, Any], response: Any) -> dict[str, Any]:
        calls = []
        text_parts = []
        refused = False
        for item in list(_read(response, "output", []) or []):
            item_type = _read(item, "type")
            if item_type == "function_call":
                item_status = _read(item, "status")
                if item_status not in (None, "completed"):
                    raise ProviderContractError(
                        "configured provider returned an incomplete function call")
                calls.append({
                    "call_id": _read(item, "call_id", ""),
                    "name": _read(item, "name", ""),
                    "arguments": self._parse_arguments(
                        _read(item, "arguments", "{}")),
                })
            elif item_type == "message":
                for content_item in list(_read(item, "content", []) or []):
                    content_type = _read(content_item, "type")
                    if content_type == "output_text":
                        text = _read(content_item, "text", "")
                        if not isinstance(text, str):
                            raise ProviderContractError(
                                "configured provider returned non-text content")
                        text_parts.append(text)
                    elif content_type == "refusal":
                        refused = True
        status = _read(response, "status", "completed") or "completed"
        incomplete = _read(response, "incomplete_details", {}) or {}
        incomplete_reason = _read(incomplete, "reason")
        if calls:
            finish_reason = "TOOL_CALLS"
        elif refused or incomplete_reason == "content_filter":
            finish_reason = "CONTENT_FILTER"
        elif status == "incomplete" and \
                incomplete_reason == "max_output_tokens":
            finish_reason = "LENGTH"
        elif _read(response, "error") is not None or status in {
                "failed", "cancelled", "queued", "in_progress"}:
            finish_reason = "ERROR"
        else:
            finish_reason = "STOP"
        usage = _read(response, "usage", {}) or {}
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": request["operation"],
            "finish_reason": finish_reason,
            "content": "".join(text_parts),
            "tool_calls": calls,
            "usage": {
                "input_tokens": _read(usage, "input_tokens", 0) or 0,
                "output_tokens": _read(usage, "output_tokens", 0) or 0,
            },
            "model_id": _read(
                response, "model", self._config["model_id"]),
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": _read(response, "id", ""),
                "provider_status": status,
            },
            "diagnostics": [],
        }

    def _response_contract(
            self, request: dict[str, Any], response: Any) -> dict[str, Any]:
        if self._config["api_version"] == "responses-v1":
            return self._responses_response_contract(request, response)
        return self._chat_response_contract(request, response)

    def _execute(
            self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._probe_passed:
            raise ProviderContractError(
                "configured provider must pass probe before use")
        try:
            # Validate and close the complete strict schema before even
            # constructing a configured network client.
            parameters = self._request_parameters(request)
        except ProviderContractError as error:
            error.safe_failure_code = "INVALID_PROVIDER_REQUEST"
            raise
        try:
            response = self._create(self._new_client(), parameters)
        except ProviderContractError:
            raise
        except Exception as error:
            failure = ProviderContractError(
                "configured provider request failed without exposing remote "
                "error or credentials")
            failure.safe_remote_signature = _safe_remote_signature(error)
            raise failure from None
        return self._response_contract(request, response)

    def _complete(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._execute(request)

    def _select_tools(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._execute(request)
