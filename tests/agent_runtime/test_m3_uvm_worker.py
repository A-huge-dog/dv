#!/usr/bin/env python3
"""M3 continuous UVM Worker vertical-loop integration qualification."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from application.uvm_generation import UvmGenerationInput
from adapters.eda import XceliumAdapter, XceliumRunConfiguration
from contracts.validator import load_document
from infrastructure.persistence.transcript_store import transcript_session_dir
from infrastructure.persistence.worker_state_store import WorkerStateStore
from runtime.errors import ProjectJobError
from runtime.staged_workflow import StagedProjectWorkflow
from tests.agent_runtime.test_xcelium_adapter import FAKE_XRUN


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ScriptedUvmProvider:
    provider_id = "m3-provider"
    model_id = "openai/gpt-5.6-sol"

    def __init__(self, tools):
        self.tools = list(tools)
        self.requests = []
        self.version = 0

    def select_tools(self, request):
        self.requests.append(copy.deepcopy(request))
        if not self.tools:
            raise AssertionError("M3 Provider script is exhausted")
        tool = self.tools.pop(0)
        arguments = {}
        if tool == "write_uvm_replacements":
            self.version += 1
            payload = json.loads(request["messages"][1]["content"])
            arguments = {"replacements": [{
                "logical_path": path,
                "content": "// worker candidate {}: {}\n".format(
                    self.version, path),
            } for path in payload["immutable_generation_context"][
                "generated_file_slots"]]}
        elif tool == "pause_task":
            arguments = {"reason": "resume after persisted first failure"}
        response_number = len(self.requests)
        return {
            "schema_version": "1.0",
            "request_id": request["request_id"],
            "operation": "SELECT_TOOLS",
            "finish_reason": "TOOL_CALLS", "content": "",
            "tool_calls": [{
                "call_id": "CALL.M3.{:03d}".format(response_number),
                "name": tool, "arguments": arguments,
            }],
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "model_id": self.model_id,
            "provider_metadata": {
                "provider_id": self.provider_id,
                "response_id": "RESPONSE.M3.{:03d}".format(response_number),
            },
            "diagnostics": [],
        }


class FakeWorkflow:
    def __init__(self, root, provider, runner, *, calls=20):
        self.workspace_root = root
        self.max_staged_file_bytes = 1024 * 1024
        self.max_mapping_items_per_shard = 32
        self.max_stage_revisions = 4
        self.max_total_provider_calls = calls
        self.max_total_tokens = 10000
        self.max_elapsed_seconds = 60
        self.provider = provider
        self.uvm_build_runner = runner
        self.probes = []

    def _probe_provider(self, _job_root, profile_role):
        self.probes.append(profile_role)

    def _complete(self, _profile_role, request, budget):
        response = self.provider.select_tools(request)
        budget["calls"] += 1
        budget["tokens"] += sum(response["usage"].values())
        return response


class M3UvmWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "result/jobs/JOB.PROJECT.M3.001"
        self.job.mkdir(parents=True)
        pkg = "package m3_pkg; endpackage\n"
        generated = "class baseline; endclass\n"
        binding = {
            "path": "config/uvm.yaml", "baseline_path": "unused",
            "fingerprint": "1" * 64, "document_fingerprint": "2" * 64,
            "provider_id": ScriptedUvmProvider.provider_id,
            "model_id": ScriptedUvmProvider.model_id, "auth_env": "TEST",
        }
        self.value = {
            "job_id": "JOB.PROJECT.M3.001",
            "input_fingerprint": "a" * 64,
            "rtl": {"sources": []},
            "uvm_testcase_context": {
                "files": [{
                    "logical_path": "uvm/pkg.sv", "content": pkg,
                    "fingerprint": _sha(pkg), "baseline_path": "unused",
                }, {
                    "logical_path": "uvm/generated.svh",
                    "content": generated, "fingerprint": _sha(generated),
                    "baseline_path": "unused",
                }],
                "generated_files": ["uvm/generated.svh"],
            },
            "eda": {"timeout_seconds": 30},
            "agent_profile": {
                "path": "config/profile.yaml",
                "byte_fingerprint": "3" * 64,
                "document_fingerprint": "4" * 64,
                "bindings": {
                    "initial": {"uvm": copy.deepcopy(binding)},
                    "repair": {"uvm": copy.deepcopy(binding)},
                },
            },
        }
        self.command = UvmGenerationInput(
            project_input=self.value, job_root=self.job,
            spec_evidence=[{"path": "spec.md", "lines": []}],
            stage2_artifact={"artifact_fingerprint": "b" * 64},
            logical_testcases=[{"testcase_id": "TC.1"}],
            stage2_shards=[{"shard": 1}],
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_worker(self, tools, statuses, *, calls=20, provider=None):
        active_provider = provider or ScriptedUvmProvider(tools)
        xcelium_calls = []
        remaining = list(statuses)

        def runner(_value, _job, request):
            xcelium_calls.append(copy.deepcopy(request))
            status = remaining.pop(0)
            return {
                "execution_status": status,
                "exit_code": 0 if status == "PASS" else 1,
                "stdout": "compile {}".format(status),
                "stderr": "syntax error" if status == "FAIL" else "",
                "diagnostic_codes": (
                    ["COMPILE_FAILED"] if status == "FAIL" else []),
                "logs": [],
            }

        runtime = StagedProjectWorkflow(FakeWorkflow(
            self.root, active_provider, runner, calls=calls))
        result = runtime._run_uvm_worker(self.command)
        return result, active_provider, xcelium_calls, runtime

    def load_state(self):
        return WorkerStateStore(
            job_root=self.job,
            task_path="audit/workers/DVTASK.UVM.INITIAL",
            task_id="DVTASK.UVM.INITIAL",
            worker_session_id="DVWORKER.UVM.INITIAL",
            job_id=self.value["job_id"],
            authority_fingerprint=load_document(sorted((
                self.job / "audit/workers/DVTASK.UVM.INITIAL").glob(
                    "state-*.json"))[-1])["authority_fingerprint"],
        )

    def test_fail_repair_pass_uses_one_session_and_current_fingerprints(self):
        result, provider, runs, _runtime = self.run_worker([
            "write_uvm_replacements", "run_xcelium_compile",
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ], ["FAIL", "PASS"])

        self.assertEqual("UVM_GENERATION_PASS", result.state)
        self.assertEqual(2, len(runs))
        self.assertEqual({"DVWORKER.UVM.INITIAL"}, {
            request["metadata"]["session_id"] for request in provider.requests})
        second_write = provider.requests[2]
        self.assertIn("syntax error", json.dumps(second_write["messages"]))
        self.assertEqual(result.effective_uvm_root,
                         runs[1]["effective_uvm_root"])
        evidence = load_document(self.job /
            "audit/uvm_generation/initial/attempt-002/"
            "xcelium-run-001.result.json")
        self.assertEqual(runs[1]["request_fingerprint"],
                         evidence["request_fingerprint"])
        state = self.load_state()
        self.assertEqual("SUCCEEDED", state.current["status"])
        candidate = load_document(self.job /
            "staging/generated/uvm/initial/attempt-002/candidate.json")
        self.assertEqual(candidate["candidate_fingerprint"],
                         state.current["latest_candidate_fingerprint"])
        manifest = load_document(transcript_session_dir(
            self.job, "UVM_GENERATION", "DVWORKER.UVM.INITIAL") /
            "manifest.json")
        self.assertEqual("COMPLETED", manifest["terminal"]["status"])

    def test_early_finish_is_rejected_and_budget_pause_is_not_success(self):
        result, provider, runs, _runtime = self.run_worker([
            "finish_task", "write_uvm_replacements",
            "run_xcelium_compile", "finish_task",
        ], ["PASS"])
        self.assertEqual("UVM_GENERATION_PASS", result.state)
        self.assertIn("NOT_COMPLETE", json.dumps(provider.requests[1][
            "messages"]))
        self.assertEqual(1, len(runs))

        self.tearDown()
        self.setUp()
        paused, _provider, paused_runs, _runtime = self.run_worker([
            "write_uvm_replacements", "run_xcelium_compile",
        ], ["PASS"], calls=1)
        self.assertEqual("PAUSED_BUDGET", paused.state)
        self.assertEqual([], paused_runs)
        self.assertEqual("PAUSED_BUDGET", self.load_state().current["status"])

    def test_pause_resume_does_not_repeat_first_failed_xcelium(self):
        provider = ScriptedUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile", "pause_task",
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ])
        statuses = ["FAIL", "PASS"]
        runs = []

        def runner(_value, _job, request):
            runs.append(copy.deepcopy(request))
            status = statuses.pop(0)
            return {
                "execution_status": status,
                "exit_code": 0 if status == "PASS" else 1,
                "stdout": status, "stderr": "first failure" if
                    status == "FAIL" else "", "logs": [],
            }

        runtime = StagedProjectWorkflow(FakeWorkflow(
            self.root, provider, runner))
        paused = runtime._run_uvm_worker(self.command)
        self.assertEqual("PAUSED_RETRYABLE", paused.state)
        self.assertEqual(1, len(runs))
        calls_before_resume = len(provider.requests)

        completed = runtime._run_uvm_worker(self.command)
        self.assertEqual("UVM_GENERATION_PASS", completed.state)
        self.assertEqual(2, len(runs))
        self.assertEqual(3, calls_before_resume)
        self.assertEqual(6, len(provider.requests))

    def test_old_pass_and_latest_fail_cannot_complete_new_candidate(self):
        result, provider, runs, _runtime = self.run_worker([
            "write_uvm_replacements", "run_xcelium_compile",
            "write_uvm_replacements", "finish_task",
            "run_xcelium_compile", "finish_task",
        ], ["PASS", "PASS"])
        self.assertEqual("UVM_GENERATION_PASS", result.state)
        self.assertEqual(2, len(runs))
        self.assertIn("NOT_COMPLETE", json.dumps(
            provider.requests[4]["messages"]))
        self.assertNotEqual(runs[0]["effective_uvm_root"],
                            runs[1]["effective_uvm_root"])

        self.tearDown()
        self.setUp()
        result, provider, runs, _runtime = self.run_worker([
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ], ["FAIL", "PASS"])
        self.assertEqual("UVM_GENERATION_PASS", result.state)
        self.assertEqual(2, len(runs))
        self.assertIn("XCELIUM_NOT_PASS", json.dumps(
            provider.requests[3]["messages"]))

    def test_provider_pause_and_xcelium_block_are_typed_non_success_states(self):
        provider = ScriptedUvmProvider(["write_uvm_replacements"])

        class UnavailableWorkflow(FakeWorkflow):
            def _complete(self, _profile_role, _request, _budget):
                raise ProjectJobError("BLOCKED_TOOL", "provider unavailable")

        runtime = StagedProjectWorkflow(UnavailableWorkflow(
            self.root, provider, lambda *_args: None))
        paused = runtime._run_uvm_worker(self.command)
        self.assertEqual("PAUSED_RETRYABLE", paused.state)
        self.assertEqual("PAUSED_RETRYABLE", self.load_state().current["status"])

        self.tearDown()
        self.setUp()
        provider = ScriptedUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile"])

        def unavailable_xcelium(*_args):
            raise ProjectJobError("BLOCKED_TOOL", "xcelium unavailable")

        runtime = StagedProjectWorkflow(FakeWorkflow(
            self.root, provider, unavailable_xcelium))
        blocked = runtime._run_uvm_worker(self.command)
        self.assertEqual("BLOCKED_TOOL", blocked.state)
        self.assertEqual("BLOCKED_TOOL", self.load_state().current["status"])

    def test_candidate_attempts_are_bounded_by_worker_action_budget(self):
        tools = []
        for _ in range(3):
            tools.extend([
                "write_uvm_replacements", "run_xcelium_compile"])
        tools.append("write_uvm_replacements")

        paused, provider, runs, _runtime = self.run_worker(
            tools, ["FAIL", "FAIL", "FAIL"])

        self.assertEqual("PAUSED_BUDGET", paused.state)
        self.assertEqual("PAUSED_BUDGET", self.load_state().current["status"])
        self.assertEqual(7, len(provider.requests))
        self.assertEqual(3, len(runs))
        self.assertFalse((
            self.job /
            "staging/generated/uvm/initial/attempt-004/candidate.json"
        ).exists())

    def test_fourth_candidate_is_legal_when_action_budget_remains(self):
        completed, provider, runs, _runtime = self.run_worker([
            "write_uvm_replacements", "write_uvm_replacements",
            "write_uvm_replacements", "write_uvm_replacements",
            "run_xcelium_compile", "finish_task",
        ], ["PASS"])

        self.assertEqual("UVM_GENERATION_PASS", completed.state)
        self.assertEqual(6, len(provider.requests))
        self.assertEqual(4, runs[0]["attempt"])
        self.assertEqual("BUILD", runs[0]["phase"])
        self.assertTrue((
            self.job /
            "staging/generated/uvm/initial/attempt-004/candidate.json"
        ).is_file())

    def test_finish_reloads_worker_authority_from_disk(self):
        test_case = self

        class TamperingProvider(ScriptedUvmProvider):
            def select_tools(self, request):
                response = super().select_tools(request)
                if response["tool_calls"][0]["name"] == "finish_task":
                    path = (test_case.job /
                            "audit/uvm_generation/initial/worker-authority.json")
                    authority = load_document(path)
                    authority["baseline_uvm_root"] = "f" * 64
                    path.write_text(
                        json.dumps(authority, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
                return response

        provider = TamperingProvider([
            "write_uvm_replacements", "run_xcelium_compile",
            "finish_task", "pause_task",
        ])
        paused, provider, runs, _runtime = self.run_worker(
            [], ["PASS"], provider=provider)

        self.assertEqual("PAUSED_RETRYABLE", paused.state)
        self.assertEqual(1, len(runs))
        self.assertIn("STALE_EVIDENCE", json.dumps(
            provider.requests[-1]["messages"]))
        self.assertNotEqual("SUCCEEDED", self.load_state().current["status"])

    def test_evidence_without_working_state_is_rejected(self):
        provider = ScriptedUvmProvider(["write_uvm_replacements"])
        runtime = StagedProjectWorkflow(FakeWorkflow(
            self.root, provider, lambda *_args: None))
        runtime.uvm_generation_handler._append_checkpoint(
            self.command, "UVM_GENERATION_PENDING", 0, "CALL_PROVIDER")

        with self.assertRaises(ProjectJobError) as caught:
            runtime._run_uvm_worker(self.command)

        self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        self.assertEqual([], provider.requests)
        self.assertFalse((
            self.job / "audit/workers/DVTASK.UVM.INITIAL").exists())

    def test_build_evidence_recovers_after_adapter_side_effect_crash(self):
        class SimulatedCrash(BaseException):
            pass

        tools = self.root / "tools"
        tools.mkdir()
        xrun = tools / "xrun"
        xrun.write_text(FAKE_XRUN, encoding="utf-8")
        xrun.chmod(0o755)
        adapter = XceliumAdapter(
            self.root, self.root / "result", self.value["job_id"], xrun,
            "XCELIUMENV.TEST.24_09", {
                "PATH": "{}{}{}".format(tools, os.pathsep, "/usr/bin"),
                "LC_ALL": "C",
            }, timeout_seconds=30)
        adapter_calls = []

        def runner(_value, _job, request):
            adapter_calls.append(copy.deepcopy(request))
            records = [*request["sources"], *request["framework_sources"]]
            sources = tuple(
                "result/jobs/{}/{}".format(self.value["job_id"], item["path"])
                for item in records)
            include_dirs = tuple(sorted({
                str(Path(source).parent) for source in sources
            }))
            result = adapter.build_only(
                "UVM.INITIAL.ATTEMPT001.RUN001",
                XceliumRunConfiguration(
                    sources=sources, include_dirs=include_dirs,
                    top=request["top"], uvm=True, timeout_seconds=30))
            raise SimulatedCrash("after Xcelium BUILD side effect")

        provider = ScriptedUvmProvider([
            "write_uvm_replacements", "run_xcelium_compile", "finish_task",
        ])
        runtime = StagedProjectWorkflow(FakeWorkflow(
            self.root, provider, runner))
        with self.assertRaises(SimulatedCrash):
            runtime._run_uvm_worker(self.command)
        self.assertEqual(1, len(adapter_calls))

        def must_not_rerun(*_args):
            self.fail("persisted BUILD evidence must prevent Xcelium replay")

        runtime.workflow.uvm_build_runner = must_not_rerun
        completed = runtime._run_uvm_worker(self.command)

        self.assertEqual("UVM_GENERATION_PASS", completed.state)
        self.assertEqual(1, len(adapter_calls))
        self.assertEqual("SUCCEEDED", self.load_state().current["status"])


if __name__ == "__main__":
    unittest.main()
