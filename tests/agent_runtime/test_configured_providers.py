#!/usr/bin/env python3
"""Configured OpenAI-compatible provider tests for the Project Job slice."""
from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adapters.llm import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    ProviderContractError,
)
from contracts.validator import load_document, load_schema
from runtime.staged_workflow import (
    STAGE1, STAGE2, STAGE3, _generation_tools)
from agents.project_tools import (
    ORCHESTRATOR_READ_TOOLS, STAGE_READ_TOOLS, read_tool_definitions)
from domain.review import provider_review_request
from scripts.run_project_job import configured_provider


ROOT = Path(__file__).resolve().parents[2]
SECRET = "test-credential-never-persist"


def config(model_id="any-configured-model"):
    return {
        "schema_version": "2.0",
        "provider_kind": "OPENAI_COMPATIBLE",
        "provider_id": "configured-provider-test",
        "model_id": model_id,
        "endpoint": "https://models.example.test/v1",
        "auth_env": "DV_TEST_PROVIDER_AUTH",
        "api_version": "chat-completions-v1",
        "tool_calls_required": True,
        "store": False,
        "timeout_seconds": 5,
        "max_retries": 0,
        "max_output_tokens": 256,
    }


def responses_config(model_id="gpt-5.6-sol"):
    value = config(model_id)
    value.update({
        "provider_kind": "OPENAI_API",
        "provider_id": "openai-primary",
        "endpoint": "https://api.openai.com/v1",
        "auth_env": "OPENAI_API_KEY",
        "api_version": "responses-v1",
        "reasoning_effort": "medium",
    })
    return value


def chat_text(model_id, content="done"):
    return SimpleNamespace(
        id="chat_text",
        model=model_id,
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=content, tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
    )


def chat_tool(
        model_id, name="dv_agent_capability_probe",
        arguments="{}", call_id="call_probe",
):
    call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    return SimpleNamespace(
        id="chat_tool",
        model=model_id,
        choices=[SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[call]))],
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=3),
    )


def responses_text(model_id, content="done"):
    return SimpleNamespace(
        id="response_text",
        model=model_id,
        status="completed",
        error=None,
        incomplete_details=None,
        output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text=content)])],
        usage=SimpleNamespace(input_tokens=11, output_tokens=4),
    )


def responses_tool(
        model_id, name="dv_agent_capability_probe",
        arguments="{}", call_id="call_probe", status="completed",
):
    return SimpleNamespace(
        id="response_tool",
        model=model_id,
        status="completed",
        error=None,
        incomplete_details=None,
        output=[SimpleNamespace(
            type="function_call",
            status=status,
            call_id=call_id,
            name=name,
            arguments=arguments,
        )],
        usage=SimpleNamespace(input_tokens=13, output_tokens=5),
    )


class RemoteError(RuntimeError):
    status_code = 400
    code = "invalid_value"
    param = "max_tokens"


class FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(copy.deepcopy(kwargs))
        if not self.owner.queued:
            raise AssertionError("fake completion queue exhausted")
        value = self.owner.queued.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeResponses(FakeCompletions):
    pass


class FakeClient:
    def __init__(self, queued):
        self.queued = list(queued)
        self.requests = []
        self.chat = SimpleNamespace(completions=FakeCompletions(self))
        self.responses = FakeResponses(self)
        self.credential_matched = False

    def factory(self, **kwargs):
        self.credential_matched = kwargs.pop("api_key") == SECRET
        self.client_options = kwargs
        return self


def complete_request(request_id):
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "operation": "COMPLETE",
        "messages": [
            {"role": "SYSTEM", "content": "Use approved evidence only."},
            {"role": "USER", "content": "Generate the testcase."},
        ],
        "tools": [],
        "tool_choice_policy": "NONE",
        "legal_tool_names": [],
        "metadata": {"job_id": "JOB.PROJECT.TEST.001"},
    }


def tool_request(request_id="REQ.TOOL.001"):
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "operation": "SELECT_TOOLS",
        "messages": [
            {"role": "SYSTEM", "content": "Call exactly one tool."},
            {"role": "USER", "content": "Submit the result."},
        ],
        "tools": [{
            "name": "submit_result",
            "description": "Submit one empty test result.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        }],
        "tool_choice_policy": "REQUIRED",
        "legal_tool_names": ["submit_result"],
        "metadata": {"job_id": "JOB.PROJECT.TEST.001"},
    }


class ConfiguredProviderTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_auth_fails_closed_without_client(self):
        called = []
        provider = OpenAICompatibleProvider(
            config(), lambda **kwargs: called.append(kwargs))
        result = provider.probe()
        self.assertEqual("FAIL", result["status"])
        self.assertEqual(
            "PROVIDER_CONFIG_MISSING",
            result["diagnostics"][0]["code"])
        self.assertEqual([], called)
        with self.assertRaises(ProviderContractError):
            provider.complete(complete_request("REQ.NO.PROBE"))

    @patch.dict(
        os.environ, {"DV_TEST_PROVIDER_AUTH": SECRET}, clear=True)
    def test_probe_checks_model_and_function_call(self):
        model_id = "vendor-model-alpha"
        value = config(model_id)
        value["reasoning_effort"] = "none"
        fake = FakeClient([
            chat_text(model_id, "OK"),
            chat_tool(model_id),
        ])
        result = OpenAICompatibleProvider(
            value, fake.factory).probe()
        self.assertEqual("PASS", result["status"])
        self.assertEqual(2, len(fake.requests))
        self.assertEqual("auto", fake.requests[1]["tool_choice"])
        self.assertIs(
            True, fake.requests[1]["tools"][0]["function"]["strict"])
        self.assertEqual("none", fake.requests[0]["reasoning_effort"])
        self.assertEqual("none", fake.requests[1]["reasoning_effort"])
        self.assertTrue(fake.credential_matched)
        self.assertNotIn(SECRET, repr(result))

    @patch.dict(
        os.environ, {"DV_TEST_PROVIDER_AUTH": SECRET}, clear=True)
    def test_probe_failure_is_redacted(self):
        fake = FakeClient([
            RemoteError("remote response contained " + SECRET)])
        result = OpenAICompatibleProvider(config(), fake.factory).probe()
        message = result["diagnostics"][0]["message"]
        self.assertEqual("FAIL", result["status"])
        self.assertIn("type=RemoteError", message)
        self.assertIn("status=400", message)
        self.assertNotIn(SECRET, repr(result))

    @patch.dict(
        os.environ, {"OPENAI_API_KEY": SECRET}, clear=True)
    def test_responses_probe_and_tool_call_use_neutral_contract(self):
        model_id = "gpt-5.6-sol"
        fake = FakeClient([
            responses_text(model_id, "OK"),
            responses_tool(model_id),
            responses_tool(model_id, name="submit_result"),
            responses_text(model_id, "module generated; endmodule"),
        ])
        provider = OpenAICompatibleProvider(
            responses_config(model_id), fake.factory)
        self.assertEqual("PASS", provider.probe()["status"])
        result = provider.select_tools(tool_request())
        self.assertEqual("TOOL_CALLS", result["finish_reason"])
        self.assertEqual("submit_result", result["tool_calls"][0]["name"])
        self.assertEqual(13, result["usage"]["input_tokens"])
        self.assertEqual(5, result["usage"]["output_tokens"])
        request = fake.requests[2]
        self.assertEqual(model_id, request["model"])
        self.assertEqual("medium", request["reasoning"]["effort"])
        self.assertEqual("required", request["tool_choice"])
        self.assertFalse(request["parallel_tool_calls"])
        self.assertFalse(request["store"])
        self.assertIn("input", request)
        self.assertNotIn("messages", request)
        self.assertNotIn("max_tokens", request)
        self.assertIs(True, request["tools"][0]["strict"])
        self.assertEqual("Call exactly one tool.", request["instructions"])
        self.assertEqual([{
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "Submit the result.",
            }],
        }], request["input"])
        completion = provider.complete(
            complete_request("REQ.RESPONSES.COMPLETE"))
        self.assertEqual(
            "module generated; endmodule", completion["content"])
        self.assertEqual("STOP", completion["finish_reason"])
        self.assertEqual(11, completion["usage"]["input_tokens"])
        self.assertNotIn("tools", fake.requests[3])
        self.assertEqual(
            "Use approved evidence only.",
            fake.requests[3]["instructions"])
        self.assertEqual(
            "Generate the testcase.",
            fake.requests[3]["input"][0]["content"][0]["text"])

    @patch.dict(
        os.environ, {"OPENAI_API_KEY": SECRET}, clear=True)
    def test_persisted_probe_activates_fresh_provider_instance(self):
        model_id = "gpt-5.6-sol"
        probe_client = FakeClient([
            responses_text(model_id, "OK"),
            responses_tool(model_id),
        ])
        probe = OpenAICompatibleProvider(
            responses_config(model_id), probe_client.factory).probe()
        self.assertEqual("PASS", probe["status"])

        resumed_client = FakeClient([
            responses_tool(model_id, name="submit_result"),
        ])
        resumed = OpenAICompatibleProvider(
            responses_config(model_id), resumed_client.factory)
        resumed.restore_probe(probe)
        result = resumed.select_tools(tool_request("REQ.RESTORED.PROBE"))

        self.assertEqual("TOOL_CALLS", result["finish_reason"])
        self.assertEqual("submit_result", result["tool_calls"][0]["name"])
        self.assertEqual(1, len(resumed_client.requests))

        wrong_model = copy.deepcopy(probe)
        wrong_model["model_id"] = "different-model"
        fresh = OpenAICompatibleProvider(
            responses_config(model_id), FakeClient([]).factory)
        with self.assertRaises(ProviderContractError):
            fresh.restore_probe(wrong_model)

    def test_responses_assistant_history_uses_canonical_output_item(self):
        request = complete_request("REQ.RESPONSES.HISTORY")
        request["messages"].insert(2, {
            "role": "ASSISTANT",
            "content": "prior generated content",
        })
        translated = OpenAICompatibleProvider(
            responses_config(), lambda **kwargs: None)._request_parameters(
                request)
        assistant = translated["input"][1]
        self.assertEqual("message", assistant["type"])
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual("completed", assistant["status"])
        self.assertTrue(assistant["id"].startswith("msg_history"))
        self.assertEqual(
            "output_text", assistant["content"][0]["type"])
        self.assertEqual(
            "prior generated content", assistant["content"][0]["text"])

    @patch.dict(
        os.environ, {"OPENAI_API_KEY": SECRET}, clear=True)
    def test_responses_incomplete_function_call_fails_closed(self):
        model_id = "gpt-5.6-sol"
        fake = FakeClient([
            responses_text(model_id, "OK"),
            responses_tool(model_id),
            responses_tool(
                model_id, name="submit_result", status="incomplete"),
        ])
        provider = OpenAICompatibleProvider(
            responses_config(model_id), fake.factory)
        self.assertEqual("PASS", provider.probe()["status"])
        with self.assertRaises(ProviderContractError):
            provider.select_tools(tool_request("REQ.TOOL.INCOMPLETE"))

    @patch.dict(
        os.environ, {"DV_TEST_PROVIDER_AUTH": SECRET}, clear=True)
    def test_arbitrary_models_use_the_same_complete_contract(self):
        for model_id in (
                "vendor-model-alpha", "vendor-model-beta", "gpt-family-x"):
            with self.subTest(model_id=model_id):
                fake = FakeClient([
                    chat_text(model_id, "OK"),
                    chat_tool(model_id),
                    chat_text(model_id, "module generated; endmodule"),
                ])
                provider = OpenAICompatibleProvider(
                    config(model_id), fake.factory)
                self.assertEqual("PASS", provider.probe()["status"])
                result = provider.complete(
                    complete_request("REQ.{}.001".format(model_id)))
                self.assertEqual(
                    "module generated; endmodule", result["content"])
                self.assertEqual(model_id, fake.requests[2]["model"])
                self.assertNotIn("tools", fake.requests[2])

    def test_review_tool_schema_survives_compatible_translation(self):
        request = provider_review_request({
            "review_request_id": "REQUEST.PROJECT.REVIEW.TEST.R001",
            "job_id": "JOB.PROJECT.TEST.001",
            "review_id": "REVIEW.PROJECT.TEST.R001",
            "artifact_roots": {
                "scenario_ac_map": "0" * 64,
                "ac_testcase_map": "0" * 64,
                "testcase": "0" * 64,
            },
            "request_fingerprint": "1" * 64,
        })
        translated = OpenAICompatibleProvider(
            config(), lambda **kwargs: None)._request_parameters(request)
        self.assertEqual("auto", translated["tool_choice"])
        self.assertEqual(2, len(translated["tools"]))
        review_tool = next(
            tool for tool in request["tools"]
            if tool["name"] == "submit_staged_project_review")
        sent = OpenAICompatibleProvider._chat_tool(review_tool)
        parameters = sent["function"]["parameters"]
        issue = parameters["properties"]["findings"]["items"]
        self.assertEqual(
            {
                "severity", "suspected_origin_stage", "affected",
                "testcase_evidence", "spec_evidence",
                "problem_and_required_change",
            },
            set(issue["properties"]))
        self.assertEqual(
            {"content"},
            set(issue["properties"][
                "testcase_evidence"]["items"]["properties"]))
        self.assertEqual(
            {
                "ac_id", "status", "spec_evidence", "stimulus_evidence",
                "checker_evidence", "omission",
            },
            set(parameters["properties"][
                "ac_reviews"]["items"]["properties"]))

    def test_generation_candidate_refs_survive_compatible_translation(self):
        schema = load_document(
            ROOT / "contracts/project/scenario_ac_candidate.schema.yaml")
        candidate_tool = {
            "name": "submit_scenario_ac_candidate",
            "description": "Submit one typed candidate.",
            "input_schema": schema,
        }
        blocked_tool = {
            "name": "submit_project_stage_blocked",
            "description": "Submit one typed blocked result.",
            "input_schema": load_document(
                ROOT /
                "contracts/project/project_stage_blocked.schema.yaml"),
        }
        request = {
            "schema_version": "1.0",
            "request_id": "REQUEST.PROJECT.STAGE.TEST.R000",
            "operation": "SELECT_TOOLS",
            "messages": [
                {"role": "SYSTEM", "content": "Call exactly one tool."},
            ],
            "tools": [candidate_tool, blocked_tool],
            "tool_choice_policy": "REQUIRED",
            "legal_tool_names": [
                candidate_tool["name"], blocked_tool["name"]],
            "metadata": {"stage": "SCENARIO_AC_MAP"},
        }
        translated = OpenAICompatibleProvider(
            config(), lambda **kwargs: None)._request_parameters(request)
        self.assertEqual("auto", translated["tool_choice"])
        self.assertEqual(2, len(translated["tools"]))
        sent = OpenAICompatibleProvider._chat_tool(candidate_tool)
        parameters = sent["function"]["parameters"]
        scenario = parameters["properties"]["scenarios"]["items"]
        ac = parameters["properties"]["acceptance_criteria"]["items"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(1, parameters["properties"][
            "scenarios"]["minItems"])
        self.assertEqual(512, parameters["properties"][
            "scenarios"]["maxItems"])
        self.assertEqual(
            {
                "objective", "verification_level",
                "status", "reason", "spec_evidence",
            },
            set(scenario["properties"]))
        self.assertEqual(
            {
                "scenario_indexes", "behavior", "verification_level",
                "status", "reason", "spec_evidence",
            },
            set(ac["properties"]))
        self.assertNotIn("local_ref", scenario["properties"])
        self.assertNotIn("local_ref", ac["properties"])
        self.assertEqual(
            32, scenario["properties"][
                "spec_evidence"]["maxItems"])
        self.assertEqual(
            {"path", "line_start", "line_end"},
            set(scenario["properties"][
                "spec_evidence"]["items"]["properties"]))
        self.assertNotIn("schema_version", parameters["properties"])
        self.assertEqual(
            "urn:dv:project:scenario-ac-candidate:4.0",
            schema["$id"])

    def test_all_project_tool_contexts_pass_recursive_strict_gate(self):
        contexts = []
        for stage in (STAGE1, STAGE2, STAGE3):
            contexts.extend(_generation_tools(stage))
        contexts.append({
            "name": "submit_staged_project_review",
            "description": "Submit review.",
            "input_schema": load_schema(
                "project_testcase_review_candidate"),
        })
        contexts.extend(read_tool_definitions(ORCHESTRATOR_READ_TOOLS))
        contexts.append({
            "name": "submit_repair_plan",
            "description": "Submit plan.",
            "input_schema": load_schema("project_repair_plan_candidate"),
        })
        for stage, contract in (
                ("stage1", "project_stage1_replacement_candidate"),
                ("stage2", "project_stage2_replacement_candidate"),
                ("stage3", "project_stage3_replacement_candidate")):
            contexts.extend(read_tool_definitions(STAGE_READ_TOOLS))
            contexts.append({
                "name": "submit_{}_replacement".format(stage),
                "description": "Submit replacement.",
                "input_schema": load_schema(contract),
            })
        self.assertEqual(38, len(contexts))

        def contains(value, key):
            if isinstance(value, dict):
                return key in value or any(
                    contains(item, key) for item in value.values())
            if isinstance(value, list):
                return any(contains(item, key) for item in value)
            return False

        for tool in contexts:
            with self.subTest(tool=tool["name"]):
                chat = OpenAICompatibleProvider._chat_tool(tool)
                responses = OpenAICompatibleProvider._responses_tool(tool)
                self.assertIs(True, chat["function"]["strict"])
                self.assertIs(True, responses["strict"])
                self.assertFalse(contains(chat, "uniqueItems"))
        stage2 = next(
            item for item in contexts
            if item["name"] == "submit_ac_testcase_candidate")
        translated = OpenAICompatibleProvider._chat_tool(stage2)
        self.assertEqual(
            2048, translated["function"]["parameters"]["properties"]
            ["logical_testcases"]["maxItems"])

    @patch.dict(
        os.environ, {"DV_TEST_PROVIDER_AUTH": SECRET}, clear=True)
    def test_nested_non_strict_object_fails_before_client_construction(self):
        called = []
        provider = OpenAICompatibleProvider(
            config(), lambda **kwargs: called.append(kwargs))
        provider._probe_passed = True
        request = tool_request("REQ.NESTED.NONSTRICT")
        request["tools"][0]["input_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["payload"],
            "properties": {"payload": {"type": "object"}},
        }
        with self.assertRaisesRegex(
                ProviderContractError,
                "parameters.properties.payload") as caught:
            provider.select_tools(request)
        self.assertEqual(
            "INVALID_PROVIDER_REQUEST",
            caught.exception.safe_failure_code)
        self.assertEqual([], called)

    def test_legacy_and_generic_provider_configs_are_accepted(self):
        for name, model_id, max_output_tokens in (
                ("dashscope_qwen.yaml", "qwen3.7-max", 65536),
                ("dashscope_deepseek.yaml", "deepseek-v4-pro", 393216)):
            with self.subTest(name=name):
                value = load_document(ROOT / "config/llm" / name)
                provider = OpenAICompatibleProvider(
                    value, lambda **kwargs: None)
                self.assertEqual(model_id, value["model_id"])
                self.assertEqual(
                    max_output_tokens, value["max_output_tokens"])
                self.assertEqual(value["provider_id"], provider.provider_id)

        generic = config("replaceable-model-id")
        provider = OpenAICompatibleProvider(
            generic, lambda **kwargs: None)
        self.assertEqual("configured-provider-test", provider.provider_id)

        invalid = []
        for field, value in (
                ("provider_kind", ""),
                ("endpoint", "http://models.example.test/v1"),
                ("endpoint", "https://user:pass@models.example.test/v1"),
                ("endpoint", "https://models.example.test/v1?secret=value"),
                ("auth_env", "NONE"),
                ("api_version", "unsupported-v1"),
                ("reasoning_effort", "unbounded"),
                ("store", True),
                ("api_key", SECRET)):
            item = config()
            item[field] = value
            invalid.append(item)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ProviderConfigError):
                    OpenAICompatibleProvider(value, lambda **kwargs: None)

        response_value = responses_config()
        provider = OpenAICompatibleProvider(
            response_value, lambda **kwargs: None)
        self.assertEqual("openai-primary", provider.provider_id)

        legacy_response = load_document(
            ROOT / "config/llm/dashscope_qwen.yaml")
        legacy_response["api_version"] = "responses-v1"
        with self.assertRaises(ProviderConfigError):
            OpenAICompatibleProvider(
                legacy_response, lambda **kwargs: None)

        response_with_vendor_field = responses_config()
        response_with_vendor_field["enable_thinking"] = False
        with self.assertRaises(ProviderConfigError):
            OpenAICompatibleProvider(
                response_with_vendor_field, lambda **kwargs: None)

    def test_active_submission_uses_the_unified_agent_profile(self):
        provider_config = load_document(
            ROOT / "config/llm/openrouter_primary.yaml")
        submission = load_document(ROOT.parent / "axi_lite_sram_project_job.yaml")
        self.assertEqual("OPENROUTER", provider_config["provider_kind"])
        self.assertEqual("openrouter-primary", provider_config["provider_id"])
        self.assertEqual("openai/gpt-5.6-sol", provider_config["model_id"])
        self.assertEqual(
            "https://openrouter.ai/api/v1", provider_config["endpoint"])
        self.assertRegex(
            provider_config["auth_env"], r"^[A-Z][A-Z0-9_]*$")
        self.assertEqual("responses-v1", provider_config["api_version"])
        self.assertEqual("medium", provider_config["reasoning_effort"])
        self.assertEqual(
            "dv/config/agents/project_default.yaml",
            submission["agent_profile"])
        profile = load_document(ROOT / "config/agents/project_default.yaml")
        self.assertEqual(
            "dv/config/llm/openrouter_primary.yaml",
            profile["initial"]["stage1"])
        self.assertEqual(
            "dv/config/llm/openrouter_primary.yaml",
            profile["repair"]["orchestrator"])
        self.assertEqual(
            "dv/config/llm/openrouter_gpt_5_6_terra.yaml",
            profile["repair"]["stage3"])

    def test_runtime_factory_does_not_filter_provider_or_model_names(self):
        path = ROOT / "tests/agent_runtime/.provider-config.test.yaml"
        value = config("runtime-selected-model")
        value["provider_kind"] = "CUSTOM_VENDOR"
        try:
            path.write_text(
                __import__("yaml").safe_dump(value, sort_keys=False),
                encoding="utf-8")
            provider = configured_provider(path)
            self.assertIsInstance(provider, OpenAICompatibleProvider)
            self.assertEqual(value["provider_id"], provider.provider_id)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
