#!/usr/bin/env python3
"""Run a standalone, test-only review of a source Stage 3 candidate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

DV_ROOT = Path(__file__).resolve().parents[1]
if str(DV_ROOT) not in sys.path:
    sys.path.insert(0, str(DV_ROOT))
ROOT = Path(__file__).resolve().parents[2]

from adapters.llm import OpenAICompatibleProvider, ProviderConfigError
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from runtime.recovery import stop_result
from runtime.standalone_stage3 import load_stage3_provider_config
from runtime.standalone_reviewer import (StandaloneStage3ReviewerWorkflow,
                                          validate_stage3_reviewer_submission)


def main() -> int:
    parser = argparse.ArgumentParser(description="Independently review one immutable Stage 3 source candidate")
    parser.add_argument("--reviewer-input", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = args.reviewer_input.read_bytes()
        submission = validate_stage3_reviewer_submission(yaml.safe_load(raw.decode("utf-8")))
        provider = OpenAICompatibleProvider(load_stage3_provider_config(ROOT, submission["reviewer_config"]))
        result = StandaloneStage3ReviewerWorkflow(ProjectJobWorkflow(
            ROOT, ROOT / "result", reviewer_provider=provider)).run(submission, raw)
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except ProjectJobError as error:
        print(json.dumps(stop_result(
            error.code, str(error)), sort_keys=True), file=sys.stderr)
        return 2
    except (ProviderConfigError, KeyError, OSError, ValueError, yaml.YAMLError):
        print(json.dumps(stop_result(
            "BLOCKED_INPUT", "Reviewer input could not be loaded safely"),
            sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
