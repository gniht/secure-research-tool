#!/usr/bin/env python3
from __future__ import annotations

"""
Core pipeline for secure research tool.

Provides deterministic stages of the research pipeline:
  - Web search (DuckDuckGo)
  - Fetch & sanitize (HTTP + HTML stripping + injection detection)
  - Schema validation (structural checks on extraction JSON)
  - Sanitizer floor enforcement (deterministic severity override)
  - Result building (confidence scoring, status determination)

AI stages (extraction and cross-source analysis) are handled by the
calling agent (Claude Code) via isolated subagents. This module provides
the security infrastructure around those AI calls.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from duckduckgo_search import DDGS

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


# --- Web Search (DuckDuckGo, no AI) ---


def execute_web_search(query: str, max_results: int = 10) -> list[dict]:
    """Run a DuckDuckGo search. Returns list of {title, url, snippet}."""
    results = DDGS().text(query, max_results=max_results)
    return [
        {"title": r["title"], "url": r["href"], "snippet": r["body"]}
        for r in results
    ]


# --- Fetch & Sanitize (no AI) ---


async def _fetch_single(client: httpx.AsyncClient, url: str) -> dict:
    """Fetch a single URL and sanitize its content. Returns a plain dict."""
    response = await client.get(url)
    response.raise_for_status()

    sanitize_result = sanitize(response.text)

    return {
        "url": url,
        "fetch_date": datetime.now(timezone.utc).isoformat(),
        "sanitized_text": sanitize_result.text,
        "original_length": sanitize_result.original_length,
        "sanitized_length": sanitize_result.sanitized_length,
        "truncated": sanitize_result.truncated,
        "injection_patterns": sanitize_result.patterns_detected,
        "warnings": sanitize_result.warnings,
    }


async def fetch_and_sanitize(urls: list[str]) -> list[dict]:
    """
    Fetch web pages at the given URLs and sanitize each one.

    No AI involved — pure HTTP fetching + deterministic sanitization.
    Failed fetches are skipped gracefully with error info.
    Returns list of dicts with sanitized text and metadata.
    """
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "SecureResearchTool/0.1 (research pipeline)"},
    ) as client:
        tasks = [_fetch_single(client, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    sources = []
    errors = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            errors.append({"url": urls[i], "error": str(result)})
        else:
            sources.append(result)

    return {"sources": sources, "errors": errors}


# --- Prompt Safety ---


def _sanitize_for_prompt(text: str, max_length: int = 500) -> str:
    """Sanitize a caller-provided string before embedding it in an agent prompt."""
    text = text[:max_length]
    text = re.sub(r"^#{1,6}\s", "", text, flags=re.MULTILINE)
    text = text.replace("---", "").replace("```", "")
    text = re.sub(r"</?system[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?system\]", "", text, flags=re.IGNORECASE)
    return text.strip()


# --- Sanitizer-AI Cross-check ---


def apply_sanitizer_floor(injection_patterns: list, extraction: dict) -> dict:
    """Enforce a deterministic severity floor based on sanitizer findings.

    If the sanitizer detected injection patterns, the researcher agent's
    anomaly log must reflect at least that level of concern. The agent can
    escalate above the floor but cannot downplay or omit what the sanitizer
    found. This prevents a successful prompt injection from silencing its
    own detection.

    Args:
        injection_patterns: patterns detected by the sanitizer for this source
        extraction: the researcher agent's full output JSON

    Returns:
        The extraction dict, possibly with additional anomaly log entries.
    """
    if not injection_patterns:
        return extraction

    anomaly_log = extraction.get("anomaly_log", [])

    has_high_or_critical = any(
        a.get("type") in ("instruction_detected", "suspicious_formatting")
        and a.get("severity") in ("high", "critical")
        for a in anomaly_log
    )

    if has_high_or_critical:
        return extraction

    extraction = dict(extraction)
    anomaly_log = list(anomaly_log)

    has_any_acknowledgment = any(
        a.get("type") in ("instruction_detected", "suspicious_formatting")
        for a in anomaly_log
    )

    if has_any_acknowledgment:
        anomaly_log.append({
            "type": "instruction_detected",
            "severity": "high",
            "description": (
                f"PIPELINE: Sanitizer detected {len(injection_patterns)} injection pattern(s). "
                "Researcher acknowledged but classified below 'high'. "
                "Severity floor enforced."
            ),
            "source_excerpt": None,
            "impulse_description": None,
            "action_taken": "Deterministic severity floor applied by pipeline",
        })
    else:
        anomaly_log.append({
            "type": "instruction_detected",
            "severity": "critical",
            "description": (
                f"PIPELINE: Sanitizer detected {len(injection_patterns)} injection pattern(s) "
                "but researcher agent logged no corresponding anomalies. "
                "Possible agent compromise."
            ),
            "source_excerpt": None,
            "impulse_description": None,
            "action_taken": "Deterministic severity floor applied by pipeline",
        })

    extraction["anomaly_log"] = anomaly_log
    return extraction


# --- Extraction Sanitization for Analyst ---


def sanitize_extraction_for_analyst(extraction: dict) -> dict:
    """Strip raw web content from an extraction before passing to the analyst.

    The analyst should never see raw source text. This keeps:
    - Extracted field values (what the analyst compares)
    - Which fields were/weren't found
    - Anomaly metadata (type, severity, count) — NOT descriptions
    - Behavioral audit structure (booleans)
    """
    sanitized = {}

    if "extraction" in extraction:
        ext = extraction["extraction"]
        sanitized["extraction"] = {
            "topic": ext.get("topic"),
            "domain": ext.get("domain"),
            "schema_id": ext.get("schema_id"),
            "fields": ext.get("fields", {}),
            "fields_not_found": ext.get("fields_not_found", []),
        }

    if "behavioral_audit" in extraction:
        audit = extraction["behavioral_audit"]
        sanitized["behavioral_audit"] = {
            "actions_taken": audit.get("actions_taken", []),
            "actions_declined_count": len(audit.get("actions_declined", [])),
            "output_check": audit.get("output_check", {}),
        }

    if "anomaly_log" in extraction:
        sanitized["anomaly_log_summary"] = {
            "count": len(extraction["anomaly_log"]),
            "by_type": {},
            "by_severity": {"low": 0, "medium": 0, "high": 0, "critical": 0},
        }
        for a in extraction["anomaly_log"]:
            atype = a.get("type", "unknown")
            sev = a.get("severity", "low")
            sanitized["anomaly_log_summary"]["by_type"][atype] = (
                sanitized["anomaly_log_summary"]["by_type"].get(atype, 0) + 1
            )
            if sev in sanitized["anomaly_log_summary"]["by_severity"]:
                sanitized["anomaly_log_summary"]["by_severity"][sev] += 1

    return sanitized


# --- Result Building (deterministic) ---


def build_result_single(
    extraction: dict,
    validation_result: dict,
    source_info: dict,
    schema: dict,
    topic: str,
    trust_level: str,
) -> dict:
    """Build result dict from a single extraction (no cross-referencing).

    Args:
        extraction: the researcher agent's full output JSON
        validation_result: output of validate_extraction() as dict
        source_info: {"url", "fetch_date", "warnings"} for the source
        schema: the domain schema
        topic: research topic string
        trust_level: "strict" | "standard" | "exploratory"
    """
    request_id = str(uuid.uuid4())

    ext = extraction.get("extraction", {})
    fields = ext.get("fields", {})
    field_sources = ext.get("field_sources", {})
    fields_not_found = ext.get("fields_not_found", [])
    anomaly_log = extraction.get("anomaly_log", [])

    base_confidence = 0.5
    single_source_penalty = -0.2

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

    anomaly_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    anomaly_patterns = []
    for a in anomaly_log:
        sev = a.get("severity", "low")
        if sev in anomaly_counts:
            anomaly_counts[sev] += 1
        atype = a.get("type", "unknown")
        anomaly_patterns.append(f"Source 1: {atype} [{sev}]")
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

    schema_valid = validation_result.get("valid", False)
    status = "validated"

    if trust_level == "strict":
        status = "rejected"
    elif trust_level == "standard":
        if anomaly_counts["critical"] > 0:
            status = "rejected"
        elif anomaly_counts["high"] > 0 or not schema_valid:
            status = "partial"
    elif trust_level == "exploratory":
        if anomaly_counts["critical"] > 0:
            status = "rejected"
        elif not schema_valid:
            status = "partial"

    missing_required = []
    for fname, fspec in schema.get("fields", {}).items():
        if fspec.get("required") and fname not in fields:
            missing_required.append(fname)
    if missing_required and status == "validated":
        status = "partial"

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
        "sources": [source_info],
        "quality_report": {
            "schema_valid": schema_valid,
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


def build_result_multi(
    analyst_output: dict,
    extractions: list[dict],
    validation_results: list[dict],
    source_infos: list[dict],
    schema: dict,
    topic: str,
    trust_level: str,
) -> dict:
    """Build result dict from analyst agent output (multi-source cross-referencing).

    Args:
        analyst_output: the analyst agent's full output JSON
        extractions: list of researcher agent outputs
        validation_results: list of validate_extraction() results as dicts
        source_infos: list of {"url", "fetch_date", "warnings"} per source
        schema: the domain schema
        topic: research topic string
        trust_level: "strict" | "standard" | "exploratory"
    """
    request_id = str(uuid.uuid4())

    validation = analyst_output.get("validation", {})
    consensus = validation.get("consensus_data", {})
    missing = validation.get("fields_missing", {})
    cross_report = validation.get("cross_source_report", {})
    anomaly_info = validation.get("anomaly_summary", {})

    data = {}
    field_details = {}
    for name, info in consensus.items():
        data[name] = info.get("value")
        field_details[name] = {
            "value": info.get("value"),
            "confidence": info.get("confidence", 0.0),
            "source_agreement": info.get("source_agreement", "unknown"),
            "notes": info.get("notes"),
        }

    overall_agreement = cross_report.get("overall_agreement", 0.5)

    if field_details:
        field_confidences = [f["confidence"] for f in field_details.values()]
        overall_confidence = sum(field_confidences) / len(field_confidences)
    else:
        overall_confidence = 0.0

    source_count = len(extractions)
    all_schema_valid = all(v.get("valid", False) for v in validation_results)

    critical_count = anomaly_info.get("critical_anomalies", 0)
    high_count = anomaly_info.get("high_anomalies", 0)

    if trust_level == "strict":
        if source_count < 3:
            status = "rejected"
        elif critical_count > 0 or anomaly_info.get("recommendation") == "reject":
            status = "rejected"
        elif high_count > 0 or not all_schema_valid or overall_agreement < 0.7:
            status = "partial"
        else:
            status = "validated"
    elif trust_level == "standard":
        if critical_count > 0 or anomaly_info.get("recommendation") == "reject":
            status = "rejected"
        elif high_count > 0 or not all_schema_valid:
            status = "partial"
        else:
            status = validation.get("status", "partial")
    else:
        if critical_count > 0:
            status = "rejected"
        elif not all_schema_valid:
            status = "partial"
        else:
            status = validation.get("status", "partial")

    missing_field_names = list(missing.keys())
    missing_required = [
        fname for fname in missing_field_names
        if schema.get("fields", {}).get(fname, {}).get("required")
    ]
    if missing_required and status == "validated":
        status = "partial"

    anomaly_patterns = anomaly_info.get("anomaly_patterns", [])
    total_anomalies = anomaly_info.get("total_anomalies_across_sources", 0)

    max_severity = None
    if critical_count > 0:
        max_severity = "critical"
    elif high_count > 0:
        max_severity = "high"
    elif total_anomalies > 0:
        max_severity = "medium"

    return {
        "request_id": request_id,
        "topic": topic,
        "domain": schema.get("domain", ""),
        "schema_id": schema.get("schema_id", ""),
        "status": status,
        "data": data,
        "confidence": round(overall_confidence, 2),
        "source_count": source_count,
        "field_details": field_details,
        "fields_missing": missing_field_names,
        "sources": source_infos,
        "quality_report": {
            "schema_valid": all_schema_valid,
            "cross_source_agreement": overall_agreement,
            "anomaly_count": total_anomalies,
            "anomaly_severity_max": max_severity,
        },
        "anomaly_summary": {
            "total": total_anomalies,
            "by_severity": {
                "critical": critical_count,
                "high": high_count,
                "medium": total_anomalies - critical_count - high_count,
                "low": 0,
            },
            "patterns": anomaly_patterns[:10],
        },
    }


# --- Agent Spec Loader ---


def get_agent_spec(agent_name: str) -> str:
    """Load an agent specification markdown file.

    Available agents: 'researcher', 'analyst', 'search'
    """
    path = _PROJECT_DIR / "agents" / f"{agent_name}.md"
    if not path.exists():
        raise ValueError(f"Unknown agent: {agent_name}")
    return path.read_text()


# --- JSON Parsing Utility ---


def parse_json_from_text(text: str) -> dict:
    """Parse JSON from agent response text, stripping markdown code blocks if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)
