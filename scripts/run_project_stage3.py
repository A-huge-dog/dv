#!/usr/bin/env python3
"""Run only Project Stage 3 in an isolated test-only Job."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


DV_ROOT = Path(__file__).resolve().parents[1]
if str(DV_ROOT) not in sys.path:
    sys.path.insert(0, str(DV_ROOT))

from adapters.llm import OpenAICompatibleProvider, ProviderConfigError
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.recovery import stop_result
from runtime.standalone_stage3 import (
    StandaloneStage3Workflow, load_stage3_provider_config,
    validate_stage3_submission,
)


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and validate only Stage 3 from an immutable source "
            "Project Job"))
    parser.add_argument("--stage3-input", type=Path, required=True)
    args = parser.parse_args()
    try:
        submission_bytes = args.stage3_input.read_bytes()
        submission = yaml.safe_load(submission_bytes.decode("utf-8"))
        submission = validate_stage3_submission(submission)
        provider_config = load_stage3_provider_config(
            ROOT, submission["generator_config"])
        provider = OpenAICompatibleProvider(
            provider_config)
        workflow = ProjectJobWorkflow(
            ROOT, ROOT / "result", provider=provider)
        result = StandaloneStage3Workflow(workflow).run(
            submission, submission_bytes)
        print(json.dumps(
            result, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except ProjectJobError as error:
        print(json.dumps(
            stop_result(error.code, str(error)),
            sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2
    except ProviderConfigError:
        print(json.dumps(stop_result(
            "INVALID_PROVIDER_CONFIG",
            "Stage 3 Generator provider configuration is invalid"),
            sort_keys=True), file=sys.stderr)
        return 2
    except (KeyError, OSError, ValueError, yaml.YAMLError):
        print(json.dumps(stop_result(
            "BLOCKED_INPUT", "Stage 3 source Job could not be loaded safely"),
            sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
