#!/usr/bin/env python3
"""
Schema validator for secure research tool.

Validates extracted data against domain schemas. No AI involved —
pure structural validation as the final defense layer.
"""

import json
import sys
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    valid: bool = True
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def add_error(self, message: str):
        self.valid = False
        self.errors.append(message)

    def add_warning(self, message: str):
        self.warnings.append(message)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def load_schema(schema_path: Path) -> dict:
    """Load and validate a domain schema file."""
    with open(schema_path) as f:
        schema = json.load(f)

    required_keys = {"schema_id", "version", "domain", "description", "fields"}
    missing = required_keys - set(schema.keys())
    if missing:
        raise ValueError(f"Schema missing required keys: {missing}")

    return schema


def validate_field_value(field_name: str, value, field_spec: dict) -> ValidationResult:
    """Validate a single field value against its schema specification."""
    result = ValidationResult()
    expected_type = field_spec["type"]

    # Type checking
    type_map = {
        "int": (int,),
        "float": (int, float),  # ints are acceptable as floats
        "bool": (bool,),
        "string": (str,),
        "enum": (str,),
    }

    if expected_type not in type_map:
        result.add_error(f"Unknown type '{expected_type}' in schema for field '{field_name}'")
        return result

    if not isinstance(value, type_map[expected_type]):
        result.add_error(
            f"Field '{field_name}': expected {expected_type}, got {type(value).__name__} ({value!r})"
        )
        return result

    # Bool check — prevent int 0/1 from passing as bool
    if expected_type == "bool" and isinstance(value, int) and not isinstance(value, bool):
        result.add_error(f"Field '{field_name}': expected bool, got int ({value})")
        return result

    # Range checking for numeric types
    if expected_type in ("int", "float") and "range" in field_spec:
        min_val, max_val = field_spec["range"]
        if value < min_val or value > max_val:
            result.add_error(
                f"Field '{field_name}': value {value} outside range [{min_val}, {max_val}]"
            )

    # String length checking
    if expected_type == "string" and "max_length" in field_spec:
        if len(value) > field_spec["max_length"]:
            result.add_error(
                f"Field '{field_name}': string length {len(value)} exceeds max {field_spec['max_length']}"
            )

    # Enum checking
    if expected_type == "enum" and "values" in field_spec:
        if value not in field_spec["values"]:
            result.add_error(
                f"Field '{field_name}': value '{value}' not in allowed values {field_spec['values']}"
            )

    # Suspicious content check for strings
    if expected_type == "string" and isinstance(value, str):
        suspicious_patterns = [
            "ignore previous",
            "ignore your",
            "system prompt",
            "you are now",
            "disregard",
            "new instructions",
            "override",
            "<script",
            "javascript:",
            "eval(",
            "exec(",
        ]
        lower_value = value.lower()
        for pattern in suspicious_patterns:
            if pattern in lower_value:
                result.add_warning(
                    f"Field '{field_name}': contains suspicious pattern '{pattern}'"
                )

    return result


def validate_extraction(extraction: dict, schema: dict) -> ValidationResult:
    """Validate a complete extraction against a domain schema."""
    result = ValidationResult()

    # Check extraction structure
    if "extraction" not in extraction:
        result.add_error("Missing top-level 'extraction' key")
        return result

    ext = extraction["extraction"]

    # Verify schema match
    if ext.get("schema_id") != schema["schema_id"]:
        result.add_error(
            f"Schema mismatch: extraction uses '{ext.get('schema_id')}', "
            f"expected '{schema['schema_id']}'"
        )

    # Check required structure
    for key in ("fields", "field_sources", "fields_not_found"):
        if key not in ext:
            result.add_error(f"Missing required key '{key}' in extraction")

    if not result.valid:
        return result

    fields = ext["fields"]
    schema_fields = schema["fields"]

    # Check for unexpected fields (not in schema)
    unexpected = set(fields.keys()) - set(schema_fields.keys())
    if unexpected:
        result.add_error(f"Unexpected fields not in schema: {unexpected}")

    # Validate each provided field
    for field_name, value in fields.items():
        if field_name not in schema_fields:
            continue  # already flagged above
        field_result = validate_field_value(field_name, value, schema_fields[field_name])
        result.errors.extend(field_result.errors)
        result.warnings.extend(field_result.warnings)
        if not field_result.valid:
            result.valid = False

    # Check required fields
    for field_name, field_spec in schema_fields.items():
        if field_spec.get("required", False):
            if field_name not in fields:
                result.add_error(f"Required field '{field_name}' missing from extraction")

    # Check behavioral audit exists and is structurally valid
    if "behavioral_audit" not in extraction:
        result.add_error("Missing 'behavioral_audit' — extraction lacks mandatory security checkpoint")
    else:
        audit = extraction["behavioral_audit"]
        for key in ("actions_taken", "actions_declined", "output_check"):
            if key not in audit:
                result.add_error(f"Behavioral audit missing required key '{key}'")

        # Verify output_check flags — if the agent reported a problem, trust it
        output_check = audit.get("output_check", {})
        if output_check.get("fields_are_schema_only") is False:
            result.add_error("Agent self-reported: output contains fields not in schema")
        if output_check.get("output_structure_matches_spec") is False:
            result.add_error("Agent self-reported: output structure does not match spec")
        if output_check.get("no_extra_content") is False:
            result.add_error("Agent self-reported: output contains extra content")

    # Check anomaly log exists
    if "anomaly_log" not in extraction:
        result.add_warning("Missing 'anomaly_log' — cannot assess extraction safety")

    # Check anomaly severities
    if "anomaly_log" in extraction:
        for anomaly in extraction["anomaly_log"]:
            if anomaly.get("severity") == "critical":
                result.add_warning(
                    f"Critical anomaly detected: {anomaly.get('description', 'no description')}"
                )

    return result


def validate_file(extraction_path: Path, schema_path: Path) -> ValidationResult:
    """Validate an extraction file against a schema file."""
    schema = load_schema(schema_path)

    with open(extraction_path) as f:
        extraction = json.load(f)

    return validate_extraction(extraction, schema)


def main():
    if len(sys.argv) < 3:
        print("Usage: schema_validator.py <extraction.json> <schema.json>")
        sys.exit(1)

    extraction_path = Path(sys.argv[1])
    schema_path = Path(sys.argv[2])

    if not extraction_path.exists():
        print(f"Error: extraction file not found: {extraction_path}")
        sys.exit(1)

    if not schema_path.exists():
        print(f"Error: schema file not found: {schema_path}")
        sys.exit(1)

    result = validate_file(extraction_path, schema_path)

    print(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.valid else 1)


if __name__ == "__main__":
    main()
