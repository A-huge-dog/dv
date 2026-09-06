#!/usr/bin/env python3
"""One-YAML bootstrap, recovery, and tamper tests for PJ-002."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from contracts.validator import accepted, load_document, validate
from runtime.errors import ProjectJobError
from runtime.project_job import (
    ProjectJobWorkflow,
    validate_project_input,
)
from domain.artifacts import project_input_fingerprint
try:
    from test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)
except ModuleNotFoundError:
    from tests.agent_runtime.test_project_job_workflow import (
        FakeProvider, FakeReviewerProvider, ProjectJobWorkflowTests)


class ProjectJobBootstrapTests(ProjectJobWorkflowTests):
    """Reuse the Tiny fixture while loading only bootstrap tests."""

    @staticmethod
    def submission_bytes(value):
        return yaml.safe_dump(
            value, sort_keys=False, allow_unicode=True).encode("utf-8")

    def test_one_yaml_builds_complete_immutable_internal_manifest(self):
        submission = self.project_input()
        raw = self.submission_bytes(submission)
        self.assertNotIn(b"fingerprint", raw)
        self.assertNotIn(b"\ntestcase:", raw)
        self.assertNotIn(b"pass_marker", raw)
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider())

        manifest = workflow.bootstrap_handler.handle(submission, raw)
        self.assertEqual("6.0", manifest["schema_version"])
        self.assertEqual(
            "PROJECT_INPUT_MANIFEST", manifest["manifest_kind"])
        self.assertTrue(accepted(validate("project_job_input", manifest)))
        self.assertEqual(
            manifest["input_fingerprint"],
            project_input_fingerprint(manifest))
        self.assertRegex(
            manifest["testcase"]["top"],
            r"^tb_project_[0-9a-f]{16}$")
        self.assertRegex(
            manifest["testcase"]["pass_marker"],
            r"^DV_PROJECT_[A-F0-9]{16}_PASS$")
        job_root = (
            self.root / "result/jobs/JOB.PROJECT.TINY.001")
        self.assertEqual(
            raw,
            (job_root /
             "input_baseline/project_job_submission.yaml").read_bytes())
        self.assertEqual(
            (self.root / "spec.md").read_bytes(),
            (self.root / manifest["spec"]["sources"][0][
                "baseline_path"]).read_bytes())
        self.assertEqual(
            (self.root / "tiny.sv").read_bytes(),
            (self.root / manifest["rtl"]["sources"][0][
                "baseline_path"]).read_bytes())
        persisted = load_document(
            job_root / "input_baseline/project_input_manifest.json")
        self.assertEqual(manifest, persisted)
        self.assertNotIn("providers", manifest)
        self.assertEqual(
            {"initial", "repair", "review"},
            set(manifest["agent_profile"]["bindings"]))
        profile_snapshot = job_root / manifest["agent_profile"][
            "baseline_path"]
        self.assertEqual(
            (self.root / submission["agent_profile"]).read_bytes(),
            profile_snapshot.read_bytes())
        for section, roles in manifest["agent_profile"]["bindings"].items():
            for role, binding in roles.items():
                with self.subTest(section=section, role=role):
                    self.assertEqual(
                        (self.root / binding["path"]).read_bytes(),
                        (job_root / binding["baseline_path"]).read_bytes())

        checkpoint = workflow.start(submission, raw)
        self.assertEqual(
            "AWAITING_SCENARIO_ROUTING", checkpoint["state"])
        self.assertEqual(checkpoint, workflow.start(submission, raw))
        self.assertEqual(1, workflow.provider.calls)
        self.assertEqual(0, workflow.reviewer_provider.calls)

    def test_only_profile_mapping_switches_one_initial_stage(self):
        alternate = load_document(self.root / "config/generator.yaml")
        alternate["provider_id"] = "fake-stage2-provider"
        alternate["model_id"] = "fake-stage2-model"
        (self.root / "config/stage2.yaml").write_text(
            yaml.safe_dump(alternate, sort_keys=False), encoding="utf-8")
        profile_path = self.root / "config/agents.yaml"
        profile = load_document(profile_path)
        profile["initial"]["stage2"] = "config/stage2.yaml"
        profile_path.write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

        class Stage2Provider(FakeProvider):
            provider_id = "fake-stage2-provider"
            model_id = "fake-stage2-model"

        default = FakeProvider()
        stage2 = Stage2Provider()
        reviewer = FakeReviewerProvider()
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result", default, reviewer,
            role_providers={"initial.stage2": stage2})
        submission = self.project_input()
        manifest = workflow.bootstrap_handler.handle(submission)
        self.assertEqual(
            "fake-stage2-model",
            manifest["agent_profile"]["bindings"]["initial"]["stage2"][
                "model_id"])
        self.assertEqual(
            "fake-project-model",
            manifest["agent_profile"]["bindings"]["initial"]["stage1"][
                "model_id"])
        checkpoint = workflow.start(submission)
        form = load_document(
            self.root / "result/jobs" / submission["job_id"] /
            checkpoint["owner_review_path"])
        workflow.route_scenarios(
            submission, self.completed_owner_review(
                form, "AC_TESTCASE_MAP_AND_TESTCASE", ""))
        self.assertEqual(2, default.calls)
        self.assertEqual(1, stage2.calls)
        self.assertTrue(all(
            request["metadata"]["agent_profile_role"] == "initial.stage2"
            for request in stage2.requests))

    def test_all_profile_roles_resolve_independently(self):
        profile_path = self.root / "config/agents.yaml"
        profile = load_document(profile_path)
        expected = {}
        for section, roles in (
                ("initial", ("stage1", "stage2", "stage3")),
                ("repair", ("orchestrator", "stage1", "stage2", "stage3")),
                ("review", ("initial", "final"))):
            for role in roles:
                token = "{}_{}".format(section, role)
                config = load_document(self.root / "config/generator.yaml")
                config["provider_id"] = "provider-{}".format(token)
                config["model_id"] = "model-{}".format(token)
                relative = "config/{}.yaml".format(token)
                (self.root / relative).write_text(
                    yaml.safe_dump(config, sort_keys=False),
                    encoding="utf-8")
                profile[section][role] = relative
                expected[(section, role)] = (
                    config["provider_id"], config["model_id"])
        profile_path.write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        manifest = ProjectJobWorkflow(
            self.root, self.root / "result").bootstrap_handler.handle(
                self.project_input())
        for (section, role), identity in expected.items():
            with self.subTest(section=section, role=role):
                actual = manifest["agent_profile"]["bindings"][section][role]
                self.assertEqual(identity, (
                    actual["provider_id"], actual["model_id"]))

    def test_unused_profile_identity_is_checked_only_when_role_runs(self):
        alternate = load_document(self.root / "config/generator.yaml")
        alternate["provider_id"] = "different-provider"
        alternate["model_id"] = "different-model"
        (self.root / "config/stage2.yaml").write_text(
            yaml.safe_dump(alternate, sort_keys=False), encoding="utf-8")
        profile_path = self.root / "config/agents.yaml"
        profile = load_document(profile_path)
        profile["initial"]["stage2"] = "config/stage2.yaml"
        profile_path.write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result",
            FakeProvider(), FakeReviewerProvider())
        result = workflow.start(self.project_input())
        self.assertEqual("AWAITING_SCENARIO_ROUTING", result["state"])
        self.assertEqual(1, workflow.provider.probe_calls)

    def test_legacy_provider_fields_and_invalid_profiles_are_rejected(self):
        legacy = self.project_input()
        legacy.pop("agent_profile")
        legacy["providers"] = {
            "generator_config": "config/generator.yaml",
            "reviewer_config": "config/reviewer.yaml",
        }
        with self.assertRaises(ProjectJobError) as caught:
            ProjectJobWorkflow(
                self.root, self.root / "result").bootstrap_handler.handle(legacy)
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)

        profile_path = self.root / "config/agents.yaml"
        profile_path.write_text(
            "schema_version: '1.0'\n"
            "profile_id: PROJECT_AGENT_PROFILE.TEST\n"
            "initial:\n  stage1: config/generator.yaml\n"
            "  stage1: config/reviewer.yaml\n"
            "  stage2: config/generator.yaml\n"
            "  stage3: config/generator.yaml\n"
            "repair:\n  orchestrator: config/generator.yaml\n"
            "  stage1: config/generator.yaml\n"
            "  stage2: config/generator.yaml\n"
            "  stage3: config/generator.yaml\n"
            "review:\n  initial: config/reviewer.yaml\n"
            "  final: config/reviewer.yaml\n",
            encoding="utf-8")
        invalid = self.project_input()
        invalid["job_id"] = "JOB.PROJECT.TINY.BADPROFILE"
        with self.assertRaises(ProjectJobError) as duplicate:
            ProjectJobWorkflow(
                self.root, self.root / "result").bootstrap_handler.handle(invalid)
        self.assertEqual("INVALID_AGENT_PROFILE", duplicate.exception.code)

    def test_existing_job_rejects_profile_drift(self):
        submission = self.project_input()
        workflow = ProjectJobWorkflow(self.root, self.root / "result")
        manifest = workflow.bootstrap_handler.handle(submission)
        profile_path = self.root / submission["agent_profile"]
        original = profile_path.read_bytes()
        profile_path.write_bytes(original + b"\n# drift\n")
        try:
            with self.assertRaises(ProjectJobError) as caught:
                validate_project_input(manifest, self.root)
            self.assertEqual("STALE_EVIDENCE", caught.exception.code)
        finally:
            profile_path.write_bytes(original)

    def test_submission_negative_paths_fields_and_source_collisions(self):
        cases = []
        missing = self.project_input()
        missing["job_id"] = "JOB.PROJECT.TINY.MISSING"
        missing["spec"]["sources"] = ["missing.md"]
        cases.append(("BLOCKED_INPUT", missing))

        absolute = self.project_input()
        absolute["job_id"] = "JOB.PROJECT.TINY.ABSOLUTE"
        absolute["spec"]["sources"] = ["/tmp/spec.md"]
        cases.append(("TOOL_PERMISSION_DENIED", absolute))

        parent = self.project_input()
        parent["job_id"] = "JOB.PROJECT.TINY.PARENT"
        parent["spec"]["sources"] = ["../spec.md"]
        cases.append(("TOOL_PERMISSION_DENIED", parent))

        hidden = self.project_input()
        hidden["job_id"] = "JOB.PROJECT.TINY.HIDDEN"
        hidden["spec"]["sources"] = ["private/.secret.md"]
        cases.append(("TOOL_PERMISSION_DENIED", hidden))

        duplicate = self.project_input()
        duplicate["job_id"] = "JOB.PROJECT.TINY.DUPLICATE"
        duplicate["spec"]["sources"] = ["spec.md", "spec.md"]
        cases.append(("INVALID_SCHEMA", duplicate))

        overlap = self.project_input()
        overlap["job_id"] = "JOB.PROJECT.TINY.OVERLAP"
        overlap["rtl"]["sources"] = ["spec.md"]
        cases.append(("CONFLICTING_SOURCE", overlap))

        behavior = self.project_input()
        behavior["job_id"] = "JOB.PROJECT.TINY.BEHAVIOR"
        behavior["spec"]["decisions"] = []
        cases.append(("BEHAVIOR_SIDECAR_FORBIDDEN", behavior))

        credential = self.project_input()
        credential["job_id"] = "JOB.PROJECT.TINY.CREDENTIAL"
        credential["api_key"] = "must-not-be-accepted"
        cases.append(("INVALID_SCHEMA", credential))

        for expected, submission in cases:
            with self.subTest(expected=expected, job=submission["job_id"]):
                workflow = ProjectJobWorkflow(
                    self.root, self.root / "result")
                with self.assertRaises(ProjectJobError) as error:
                    workflow.bootstrap_handler.handle(submission)
                self.assertEqual(expected, error.exception.code)
                self.assertFalse((
                    self.root / "result/jobs" /
                    submission["job_id"]).exists())

        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        (self.root / "a/spec.md").write_text(
            "first\n", encoding="utf-8")
        (self.root / "b/spec.md").write_text(
            "second\n", encoding="utf-8")
        collision = self.project_input()
        collision["job_id"] = "JOB.PROJECT.TINY.COLLISION"
        collision["spec"]["sources"] = [
            "a/spec.md", "b/spec.md"]
        with self.assertRaises(ProjectJobError) as error:
            ProjectJobWorkflow(
                self.root, self.root / "result").bootstrap_handler.handle(
                    collision)
        self.assertEqual("CONFLICTING_SOURCE", error.exception.code)

    def test_symlink_escape_and_non_regular_source_fail_closed(self):
        outside = Path(self.temp.name).parent / (
            Path(self.temp.name).name + "_outside.md")
        outside.write_text("outside\n", encoding="utf-8")
        try:
            (self.root / "escape.md").symlink_to(outside)
            escaped = self.project_input()
            escaped["job_id"] = "JOB.PROJECT.TINY.ESCAPE"
            escaped["spec"]["sources"] = ["escape.md"]
            with self.assertRaises(ProjectJobError) as error:
                ProjectJobWorkflow(
                    self.root, self.root / "result").bootstrap_handler.handle(
                        escaped)
            self.assertEqual(
                "TOOL_PERMISSION_DENIED", error.exception.code)
        finally:
            outside.unlink(missing_ok=True)

        (self.root / "directory.md").mkdir()
        non_regular = self.project_input()
        non_regular["job_id"] = "JOB.PROJECT.TINY.NONREGULAR"
        non_regular["spec"]["sources"] = ["directory.md"]
        with self.assertRaises(ProjectJobError) as error:
            ProjectJobWorkflow(
                self.root, self.root / "result").bootstrap_handler.handle(
                    non_regular)
        self.assertEqual("BLOCKED_INPUT", error.exception.code)

    def test_partial_bootstrap_recovery_and_unexpected_partial_rejection(self):
        submission = self.project_input()
        submission["job_id"] = "JOB.PROJECT.TINY.RECOVER"
        raw = self.submission_bytes(submission)
        job_root = self.root / "result/jobs" / submission["job_id"]
        partial_submission = (
            job_root / "input_baseline/project_job_submission.yaml")
        partial_submission.parent.mkdir(parents=True)
        partial_submission.write_bytes(raw)
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result")
        manifest = workflow.bootstrap_handler.handle(submission, raw)
        self.assertEqual(submission["job_id"], manifest["job_id"])
        self.assertTrue((
            job_root /
            "input_baseline/project_input_manifest.json").is_file())

        unexpected = self.project_input()
        unexpected["job_id"] = "JOB.PROJECT.TINY.PARTIAL.BAD"
        unexpected_root = (
            self.root / "result/jobs" / unexpected["job_id"])
        (unexpected_root / "staging").mkdir(parents=True)
        (unexpected_root / "staging/unbound.txt").write_text(
            "not baseline evidence\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as error:
            workflow.bootstrap_handler.handle(unexpected)
        self.assertEqual("PARTIAL_BOOTSTRAP", error.exception.code)

        symlinked = self.project_input()
        symlinked["job_id"] = "JOB.PROJECT.TINY.PARTIAL.SYMLINK"
        symlink_root = (
            self.root / "result/jobs" / symlinked["job_id"])
        symlink_root.mkdir(parents=True)
        outside_baseline = self.root / "outside_baseline"
        outside_baseline.mkdir()
        (symlink_root / "input_baseline").symlink_to(
            outside_baseline, target_is_directory=True)
        with self.assertRaises(ProjectJobError) as symlink_error:
            workflow.bootstrap_handler.handle(symlinked)
        self.assertEqual(
            "PARTIAL_BOOTSTRAP", symlink_error.exception.code)

        conflicting = self.project_input()
        conflicting["job_id"] = "JOB.PROJECT.TINY.PARTIAL.CONFLICT"
        conflicting_root = (
            self.root / "result/jobs" / conflicting["job_id"])
        (conflicting_root / "input_baseline/spec").mkdir(
            parents=True)
        (conflicting_root /
         "input_baseline/spec/spec.md").write_text(
             "different partial source bytes\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as conflict_error:
            workflow.bootstrap_handler.handle(conflicting)
        self.assertEqual(
            "STALE_EVIDENCE", conflict_error.exception.code)

    def test_submission_baseline_provider_and_source_tamper_are_stale(self):
        submission = self.project_input()
        raw = self.submission_bytes(submission)
        workflow = ProjectJobWorkflow(
            self.root, self.root / "result")
        manifest = workflow.bootstrap_handler.handle(submission, raw)

        changed_submission = copy.deepcopy(submission)
        changed_submission["input_authority"]["identity"] = "other.owner"
        with self.assertRaises(ProjectJobError) as different:
            workflow.bootstrap_handler.handle(
                changed_submission,
                self.submission_bytes(changed_submission))
        self.assertEqual("STALE_EVIDENCE", different.exception.code)

        baseline_source = (
            self.root / manifest["spec"]["sources"][0]["baseline_path"])
        baseline_source_bytes = baseline_source.read_bytes()
        baseline_source.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as tamper:
            validate_project_input(manifest, self.root)
        self.assertEqual("STALE_EVIDENCE", tamper.exception.code)
        baseline_source.write_bytes(baseline_source_bytes)

        provider_path = (
            self.root /
            manifest["agent_profile"]["bindings"]["initial"]["stage1"][
                "path"])
        provider_bytes = provider_path.read_bytes()
        provider_path.write_text(
            provider_path.read_text(encoding="utf-8") +
            "\n# policy drift\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as provider_drift:
            validate_project_input(manifest, self.root)
        self.assertEqual(
            "STALE_EVIDENCE", provider_drift.exception.code)
        provider_path.write_bytes(provider_bytes)

        manifest_path = (
            self.root / "result/jobs/JOB.PROJECT.TINY.001/"
            "input_baseline/project_input_manifest.json")
        manifest_bytes = manifest_path.read_bytes()
        tampered_manifest = load_document(manifest_path)
        tampered_manifest["input_fingerprint"] = "f" * 64
        manifest_path.write_text(
            json.dumps(tampered_manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        with self.assertRaises(ProjectJobError) as manifest_tamper:
            workflow.bootstrap_handler.handle(submission, raw)
        self.assertEqual(
            "STALE_EVIDENCE", manifest_tamper.exception.code)
        manifest_path.write_bytes(manifest_bytes)

        baseline_submission = (
            self.root / "result/jobs/JOB.PROJECT.TINY.001/"
            "input_baseline/project_job_submission.yaml")
        baseline_submission.write_text(
            baseline_submission.read_text(encoding="utf-8") +
            "\n# tampered\n", encoding="utf-8")
        with self.assertRaises(ProjectJobError) as submission_tamper:
            workflow.bootstrap_handler.handle(submission, raw)
        self.assertEqual(
            "STALE_EVIDENCE", submission_tamper.exception.code)

    def test_missing_yaml_cli_returns_retryable_typed_input_failure(self):
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[2] /
                "scripts/run_project_job.py"),
            "--project-input",
            str(self.root / "does-not-exist.yaml"),
        ]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            command, cwd=self.root, env=environment,
            text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(2, completed.returncode)
        diagnostic = json.loads(completed.stderr)
        self.assertEqual("PAUSED_RETRYABLE", diagnostic["status"])
        self.assertEqual("BLOCKED_INPUT", diagnostic["code"])
        self.assertNotEqual("INTERNAL_ERROR", diagnostic["code"])


def load_tests(loader, tests, pattern):
    """Do not inherit the full vertical-flow suite into this module."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(ProjectJobBootstrapTests):
        if name in ProjectJobBootstrapTests.__dict__:
            suite.addTest(ProjectJobBootstrapTests(name))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
