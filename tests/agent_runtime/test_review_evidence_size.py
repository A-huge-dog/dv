"""Reviewer evidence uses one UTF-8 byte budget throughout submission."""
import unittest

from contracts.validator import load_schema
from domain.review import _enrich_review_evidence, ReviewValidationError
from scripts.dvlib import validate_schema


class ReviewEvidenceSizeTests(unittest.TestCase):
    def test_utf8_boundary_across_evidence_contracts(self):
        report = load_schema("project_testcase_review_report")
        candidate = load_schema("project_testcase_review_candidate")
        check = load_schema("review_evidence_check")
        schemas = [
            report["$defs"]["spec_evidence"]["properties"]["snippet"],
            report["$defs"]["code_evidence"]["properties"]["snippet"],
            candidate["$defs"]["code_evidence"]["properties"]["content"],
            check["properties"]["checks"]["items"]["properties"]["content"],
        ]
        for text in ("a" * 16384, "中" * 5461 + "a"):
            for schema in schemas:
                with self.subTest(schema=schema, multibyte=text.startswith("中")):
                    self.assertEqual([], validate_schema(text, schema))
                    self.assertTrue(validate_schema(text + "a", schema))

    def test_spec_extraction_uses_utf8_bytes(self):
        selection = [{"path": "spec.md", "line_start": 1, "line_end": 1}]
        for text in ("a" * 16384, "中" * 5461 + "a"):
            result = _enrich_review_evidence(
                selection, {"spec.md": text}, ReviewValidationError)
            self.assertEqual(text, result[0]["snippet"])
            with self.assertRaises(ReviewValidationError) as caught:
                _enrich_review_evidence(
                    selection, {"spec.md": text + "a"}, ReviewValidationError)
            self.assertEqual("FILE_LIMIT_EXCEEDED", caught.exception.code)
