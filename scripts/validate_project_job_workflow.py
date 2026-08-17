#!/usr/bin/env python3
"""Run the complete AXI-Lite Project Job vertical-slice self-test."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_ROOT = ROOT / "tests/agent_runtime"
sys.path.insert(0, str(TEST_ROOT))
suite = unittest.TestSuite()
for module_name in (
        "test_configured_providers",
        "test_project_job_bootstrap",
        "test_project_job_workflow",
        "test_project_stage3",
        "test_project_stage3_reviewer",
        "test_project_job_reviewer",
        "test_project_incremental_artifacts",
        "test_oches001_repair_control",
        "test_oches002_tool_session",
        "test_oches002_project_tools",
        "test_oches002_scoped_repair",
        "test_oches002_repair_runtime",
        "test_oches002_one_yaml_runtime",
        "test_oches002_scheduler",
        "test_oches003_commit_runtime"):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module_name))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
