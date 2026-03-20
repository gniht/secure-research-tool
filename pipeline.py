#!/usr/bin/env python3
"""
Core pipeline for secure research tool.

Orchestrates the three-stage research pipeline:
  Stage 1: Fetch & sanitize (no AI)
  Stage 2: Extract via researcher agent (isolated AI)
  Stage 3: Validate & cross-reference (deterministic + analyst AI)
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_DIR = Path(__file__).parent

from validation.sanitizer import sanitize, SanitizeResult
from validation.schema_validator import validate_extraction, ValidationResult


# --- Errors ---


class ResearchError(Exception):
    """Base error for research pipeline failures."""
    pass


class SchemaError(ResearchError):
    """Caller-provided schema is invalid."""
    pass


class FetchError(ResearchError):
    """No sources could be fetched."""
    pass


class ExtractionError(ResearchError):
    """AI extraction stage failed."""
    pass


# --- Data Classes ---


@dataclass
class SanitizedSource:
    """Result of fetching and sanitizing a single web source."""
    url: str
    fetch_date: str
    sanitized_text: str
    sanitize_result: SanitizeResult


@dataclass
class ExtractionResult:
    """Result of running the researcher agent on one source."""
    source: SanitizedSource
    extraction: dict
    validation: ValidationResult


# --- Schema Validation ---


VALID_FIELD_TYPES = {"int", "float", "bool", "string", "enum"}


def validate_caller_schema(schema: dict) -> ValidationResult:
    """Validate a caller-provided schema for correctness before pipeline runs."""
    result = ValidationResult()

    required_keys = {"schema_id", "version", "domain", "description", "fields"}
    missing = required_keys - set(schema.keys())
    if missing:
        result.add_error(f"Schema missing required keys: {missing}")
        return result

    if not isinstance(schema.get("version"), int):
        result.add_error("'version' must be an integer")

    if not isinstance(schema.get("fields"), dict):
        result.add_error("'fields' must be an object")
        return result

    if len(schema["fields"]) == 0:
        result.add_error("Schema must define at least one field")
        return result

    for field_name, field_spec in schema["fields"].items():
        if not isinstance(field_spec, dict):
            result.add_error(f"Field '{field_name}': spec must be an object")
            continue

        if "type" not in field_spec:
            result.add_error(f"Field '{field_name}': missing 'type'")
            continue

        if field_spec["type"] not in VALID_FIELD_TYPES:
            result.add_error(
                f"Field '{field_name}': unknown type '{field_spec['type']}'. "
                f"Must be one of: {', '.join(sorted(VALID_FIELD_TYPES))}"
            )

        if "description" not in field_spec:
            result.add_warning(
                f"Field '{field_name}': missing 'description' — "
                "extraction agent won't know what to look for"
            )

        if field_spec["type"] in ("int", "float") and "range" in field_spec:
            r = field_spec["range"]
            if not (isinstance(r, list) and len(r) == 2):
                result.add_error(f"Field '{field_name}': 'range' must be [min, max]")

        if field_spec["type"] == "enum" and "values" not in field_spec:
            result.add_error(f"Field '{field_name}': enum type requires 'values' list")

    return result


# --- Stage 1: Fetch & Sanitize ---


async def fetch_and_sanitize(
    topic: str,
    max_sources: int,
    source_hints: list[str] | None = None,
) -> list[SanitizedSource]:
    """
    Fetch web pages about the topic and sanitize each one.

    Stage 1 of the pipeline — no AI involved.
    """
    # TODO: Implement web search + HTTP fetching
    # Needs: search mechanism (e.g., DuckDuckGo), HTTP client (httpx)
    # Each fetched page gets run through sanitize() from validation/sanitizer.py
    raise NotImplementedError(
        "Web fetching not yet implemented. "
        "Needs web search and HTTP client."
    )


# --- Prompt Safety ---


def _sanitize_for_prompt(text: str, max_length: int = 500) -> str:
    """Sanitize a caller-provided string before embedding it in an agent prompt.

    Prevents the topic or other caller strings from breaking the prompt
    structure via markdown formatting or injection-like patterns.
    """
    text = text[:max_length]
    # Strip markdown structure characters that could alter prompt sections
    text = re.sub(r"^#{1,6}\s", "", text, flags=re.MULTILINE)
    text = text.replace("---", "").replace("```", "")
    # Strip patterns that look like system/instruction markers
    text = re.sub(r"</?system[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?system\]", "", text, flags=re.IGNORECASE)
    return text.strip()


# --- Stage 2: Extract ---


def _build_researcher_prompt(sanitized_text: str, schema: dict, topic: str) -> str:
    """Build the prompt for the researcher agent."""
    agent_spec = (_PROJECT_DIR / "agents" / "researcher.md").read_text()
    safe_topic = _sanitize_for_prompt(topic)

    return (
        f"{agent_spec}\n\n"
        f"---\n\n"
        f"## Your Current Task\n\n"
        f"**Topic:** {safe_topic}\n\n"
        f"**Domain Schema:**\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"**Source Text:**\n\n{sanitized_text}\n"
    )


async def extract_from_source(
    sanitized: SanitizedSource,
    schema: dict,
    topic: str,
) -> dict:
    """
    Invoke the researcher agent on one sanitized source.

    Stage 2 — isolated AI. The agent can only read the provided text
    and output structured JSON. No tools, no web, no project access.

    Returns the extraction JSON matching the researcher agent's output format.
    """
    # TODO: Implement Claude API invocation
    # prompt = _build_researcher_prompt(sanitized.sanitized_text, schema, topic)
    # Call Claude with the prompt, parse JSON response
    raise NotImplementedError(
        "Researcher agent invocation not yet implemented. "
        "Needs Anthropic SDK to call Claude."
    )


# --- Stage 3: Validate & Cross-reference ---


def _build_analyst_prompt(
    extractions: list[ExtractionResult],
    schema: dict,
    topic: str,
    request_id: str,
    trust_level: str,
) -> str:
    """Build the prompt for the analyst agent."""
    agent_spec = (_PROJECT_DIR / "agents" / "analyst.md").read_text()
    safe_topic = _sanitize_for_prompt(topic)

    extraction_data = []
    for i, ext in enumerate(extractions, 1):
        extraction_data.append({
            "source_number": i,
            "source_url": ext.source.url,
            "extraction": ext.extraction,
            "schema_validation": ext.validation.to_dict(),
        })

    return (
        f"{agent_spec}\n\n"
        f"---\n\n"
        f"## Your Current Task\n\n"
        f"**Request ID:** {request_id}\n"
        f"**Topic:** {safe_topic}\n"
        f"**Trust Level:** {trust_level}\n\n"
        f"**Domain Schema:**\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"**Extractions to Compare:**\n```json\n{json.dumps(extraction_data, indent=2)}\n```\n"
    )


def _build_result_from_single_extraction(
    extraction: ExtractionResult,
    schema: dict,
    request_id: str,
    topic: str,
    trust_level: str,
) -> dict:
    """Build result dict when there's only one extraction (no cross-referencing needed)."""
    ext = extraction.extraction.get("extraction", {})
    fields = ext.get("fields", {})
    field_sources = ext.get("field_sources", {})
    fields_not_found = ext.get("fields_not_found", [])
    anomaly_log = extraction.extraction.get("anomaly_log", [])

    # Single-source confidence: base 0.5, no multi-source bonus, apply penalties
    base_confidence = 0.5
    single_source_penalty = -0.2

    # Build field details with single-source confidence
    field_details = {}
    for name, value in fields.items():
        field_confidence = max(0.0, min(1.0, base_confidence + single_source_penalty))
        field_details[name] = {
            "value": value,
            "confidence": field_confidence,
            "source_agreement": "single_source",
            "notes": field_sources.get(name),
        }

    overall_confidence = max(0.0, min(1.0, base_confidence + single_source_penalty))

    # Anomaly assessment
    anomaly_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    anomaly_patterns = []
    for a in anomaly_log:
        sev = a.get("severity", "low")
        if sev in anomaly_counts:
            anomaly_counts[sev] += 1
        # Sanitize the pattern description — don't pass raw web content to caller
        desc = a.get("description", "")[:150]
        anomaly_patterns.append(f"Source 1: {a.get('type', 'unknown')} — {desc}")
        # Apply confidence penalty for anomalies
        if sev == "high":
            overall_confidence = max(0.0, overall_confidence - 0.1)
        elif sev == "critical":
            overall_confidence = max(0.0, overall_confidence - 0.3)

    total_anomalies = sum(anomaly_counts.values())
    max_severity = None
    for sev in ("critical", "high", "medium", "low"):
        if anomaly_counts[sev] > 0:
            max_severity = sev
            break

    # Determine status based on trust level
    status = "validated"

    if trust_level == "strict":
        # Strict requires 3+ sources — single source always rejected
        status = "rejected"
    elif trust_level == "standard":
        if anomaly_counts["critical"] > 0:
            status = "rejected"
        elif anomaly_counts["high"] > 0 or not extraction.validation.valid:
            status = "partial"
    elif trust_level == "exploratory":
        if anomaly_counts["critical"] > 0:
            status = "rejected"
        elif not extraction.validation.valid:
            status = "partial"

    # Check for missing required fields
    missing_required = []
    for fname, fspec in schema.get("fields", {}).items():
        if fspec.get("required") and fname not in fields:
            missing_required.append(fname)
    if missing_required and status == "validated":
        status = "partial"

    # Deduplicate fields_missing
    all_missing = list(dict.fromkeys(fields_not_found + missing_required))

    return {
        "request_id": request_id,
        "topic": topic,
        "domain": schema.get("domain", ""),
        "schema_id": schema.get("schema_id", ""),
        "status": status,
        "data": fields,
        "confidence": round(overall_confidence, 2),
        "source_count": 1,
        "field_details": field_details,
        "fields_missing": all_missing,
        "sources": [{
            "url": extraction.source.url,
            "fetch_date": extraction.source.fetch_date,
            "sanitization_warnings": extraction.source.sanitize_result.warnings,
        }],
        "quality_report": {
            "schema_valid": extraction.validation.valid,
            "cross_source_agreement": None,
            "anomaly_count": total_anomalies,
            "anomaly_severity_max": max_severity,
        },
        "anomaly_summary": {
            "total": total_anomalies,
            "by_severity": anomaly_counts,
            "patterns": anomaly_patterns[:10],
        },
    }


async def validate_and_crossref(
    extractions: list[ExtractionResult],
    schema: dict,
    trust_level: str,
    request_id: str,
    topic: str,
) -> dict:
    """
    Run schema validation and cross-reference multiple extractions.

    For single extractions: deterministic validation only.
    For 2+ extractions: also invokes the analyst agent for cross-referencing.

    Returns the final result dict matching the MCP return format.
    """
    if len(extractions) == 1:
        return _build_result_from_single_extraction(
            extractions[0], schema, request_id, topic, trust_level
        )

    # Multiple extractions — invoke analyst agent for cross-referencing
    # TODO: Implement analyst agent invocation
    # prompt = _build_analyst_prompt(extractions, schema, topic, request_id, trust_level)
    # Call Claude with the prompt, parse JSON response, build result dict
    raise NotImplementedError(
        "Multi-source cross-referencing not yet implemented. "
        "Needs Anthropic SDK to invoke analyst agent."
    )


# --- Main Orchestrator ---


async def execute_research(
    topic: str,
    schema: dict,
    trust_level: str = "standard",
    max_sources: int = 3,
    source_hints: list[str] | None = None,
) -> dict:
    """
    Execute the full three-stage research pipeline.

    Returns the validated result dict matching the MCP return format.
    Raises ResearchError subclasses on failures.
    """
    VALID_TRUST_LEVELS = ("strict", "standard", "exploratory")
    if trust_level not in VALID_TRUST_LEVELS:
        raise ValueError(
            f"Invalid trust_level '{trust_level}'. "
            f"Must be one of: {', '.join(VALID_TRUST_LEVELS)}"
        )

    request_id = str(uuid.uuid4())

    # Validate schema eagerly — fail fast before any fetching
    schema_check = validate_caller_schema(schema)
    if not schema_check.valid:
        raise SchemaError(
            f"Invalid schema: {'; '.join(schema_check.errors)}"
        )

    # Stage 1: Fetch & Sanitize
    sources = await fetch_and_sanitize(topic, max_sources, source_hints)

    if not sources:
        raise FetchError(f"No sources found for topic: {topic}")

    # Stage 2: Extract (parallel across sources)
    extraction_tasks = [
        extract_from_source(source, schema, topic)
        for source in sources
    ]
    raw_extractions = await asyncio.gather(
        *extraction_tasks, return_exceptions=True
    )

    # Pair results with sources, run schema validation on each
    extractions = []
    for source, raw in zip(sources, raw_extractions):
        if isinstance(raw, Exception):
            continue  # Skip failed extractions
        validation = validate_extraction(raw, schema)
        extractions.append(ExtractionResult(
            source=source,
            extraction=raw,
            validation=validation,
        ))

    if not extractions:
        raise ExtractionError("All extraction attempts failed")

    # Stage 3: Validate & Cross-reference
    result = await validate_and_crossref(
        extractions, schema, trust_level, request_id, topic
    )

    return result
