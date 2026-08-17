"""Dependency-light hashing and schema checks used by the vertical slice."""
from __future__ import annotations

import hashlib
import json
import re


def canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_type(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def validate_schema(value, schema, where="value", _root=None):
    """Validate the JSON-Schema subset used by Project Job contracts."""
    root = schema if _root is None else _root
    reference = schema.get("$ref")
    if reference:
        if not reference.startswith("#/"):
            return ["{}: unsupported external schema reference".format(where)]
        resolved = root
        try:
            for token in reference[2:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                resolved = resolved[token]
        except (KeyError, TypeError):
            return ["{}: unresolved schema reference".format(where)]
        return validate_schema(value, resolved, where, root)
    errors = []
    expected = schema.get("type")
    if expected and not _is_type(value, expected):
        return ["{}: must be {}".format(where, expected)]
    if "const" in schema and value != schema["const"]:
        errors.append(
            "{}: must equal {!r}".format(where, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(
            "{}: value {!r} is not in enum".format(where, value))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(
                    "{}: missing required field '{}'".format(where, key))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(
                        "{}.{}: unexpected property".format(where, key))
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_schema(
                    value[key], child, "{}.{}".format(where, key), root))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("{}: fewer than minItems".format(where))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("{}: more than maxItems".format(where))
        if schema.get("uniqueItems") and len({
                json.dumps(item, sort_keys=True, ensure_ascii=False)
                for item in value}) != len(value):
            errors.append("{}: items must be unique".format(where))
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(
                    item, schema["items"],
                    "{}[{}]".format(where, index), root))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append("{}: shorter than minLength".format(where))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("{}: longer than maxLength".format(where))
        if ("pattern" in schema and
                re.match(schema["pattern"] + r"\Z", value) is None):
            errors.append("{}: does not match pattern".format(where))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("{}: below minimum".format(where))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("{}: above maximum".format(where))
    return errors
