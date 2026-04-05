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

import anthropic
import httpx
from duckduckgo_search import AsyncDDGS

_PROJECT_DIR = Path(__file__).parent

from validation.sanitizer import sanitize, SanitizeResult
from validation.schema_validator import validate_extraction, ValidationResult


# --- Model Configuration ---

SEARCH_MODEL = "claude-sonnet-4-6"
EXTRACT_MODEL = "claude-opus-4-6"
ANALYST_MODEL = "claude-opus-4-6"


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


# --- Stage 1a: URL Discovery (search agent) ---


async def _execute_web_search(query: str, max_results: int = 10) -> list[dict]:
    """Run a DuckDuckGo search. Returns list of {title, url, snippet}."""
    async with AsyncDDGS() as ddgs:
        results = await ddgs.atext(query, max_results=max_results)
    return [
        {"title": r["title"], "url": r["href"], "snippet": r["body"]}
        for r in results
    ]


def _parse_json_from_text(text: str) -> dict:
    """Parse JSON from agent response text, stripping markdown code blocks if present."""
    text = text.strip()
    if text.startswith("```"):
        # Strip code block markers
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


async def discover_sources(
    topic: str,
    max_sources: int,
    source_hints: list[str] | None = None,
) -> list[str]:
    """
    Use the search agent to find relevant URLs for a topic.

    The search agent (Sonnet) constructs search queries, evaluates results
    via DuckDuckGo, and returns the most relevant URLs.
    """
    client = anthropic.AsyncAnthropic()

    search_tool = {
        "name": "web_search",
        "description": (
            "Search the web for pages relevant to the research topic. "
            "Returns a list of results with title, URL, and snippet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to execute",
                }
            },
            "required": ["query"],
        },
    }

    agent_spec = (_PROJECT_DIR / "agents" / "search.md").read_text()
    safe_topic = _sanitize_for_prompt(topic)

    user_message = f"**Topic:** {safe_topic}\n"
    if source_hints:
        user_message += f"**Source hints:** {', '.join(source_hints)}\n"
    user_message += f"**Max URLs to return:** {max_sources}\n"

    messages = [{"role": "user", "content": user_message}]

    # Tool use loop — agent calls web_search, we execute it, repeat until done
    while True:
        response = await client.messages.create(
            model=SEARCH_MODEL,
            max_tokens=1024,
            system=agent_spec,
            tools=[search_tool],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            # Execute each tool call
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "web_search":
                    try:
                        results = await _execute_web_search(block.input["query"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(results),
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": str(e)}),
                            "is_error": True,
                        })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Agent is done — extract JSON from final response
            break

    # Parse the agent's final response
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    try:
        result = _parse_json_from_text(text)
    except (json.JSONDecodeError, ValueError):
        raise FetchError("Search agent returned invalid JSON")

    # Extract and validate URLs
    urls = []
    seen = set()
    for entry in result.get("urls", []):
        url = entry.get("url", "")
        if url.startswith(("http://", "https://")) and url not in seen:
            urls.append(url)
            seen.add(url)

    return urls[:max_sources]


# --- Stage 1b: Fetch & Sanitize (no AI) ---


async def _fetch_single(client: httpx.AsyncClient, url: str) -> SanitizedSource:
    """Fetch a single URL and sanitize its content."""
    response = await client.get(url)
    response.raise_for_status()

    sanitize_result = sanitize(response.text)

    return SanitizedSource(
        url=url,
        fetch_date=datetime.now(timezone.utc).isoformat(),
        sanitized_text=sanitize_result.text,
        sanitize_result=sanitize_result,
    )


async def fetch_and_sanitize(urls: list[str]) -> list[SanitizedSource]:
    """
    Fetch web pages at the given URLs and sanitize each one.

    No AI involved — pure HTTP fetching + deterministic sanitization.
    Failed fetches are skipped gracefully.
    """
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "SecureResearchTool/0.1 (research pipeline)"},
    ) as client:
        tasks = [_fetch_single(client, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    sources = []
    for result in results:
        if not isinstance(result, Exception):
            sources.append(result)

    return sources


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
    """Build the user message for the researcher agent.

    The agent spec (researcher.md) is sent as the system prompt separately.
    This returns only the task portion — topic, schema, and source text.
    """
    safe_topic = _sanitize_for_prompt(topic)

    return (
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

    Security boundary: the agent spec is the system prompt (trusted),
    the source text is in the user message (untrusted). No tools are
    provided, so the model cannot take actions beyond generating text.

    Returns the extraction JSON matching the researcher agent's output format.
    """
    client = anthropic.AsyncAnthropic()

    agent_spec = (_PROJECT_DIR / "agents" / "researcher.md").read_text()
    user_message = _build_researcher_prompt(
        sanitized.sanitized_text, schema, topic
    )

    response = await client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=4096,
        system=agent_spec,
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract text from response
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    if not text.strip():
        raise ExtractionError(
            f"Researcher agent returned empty response for {sanitized.url}"
        )

    try:
        extraction = _parse_json_from_text(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ExtractionError(
            f"Researcher agent returned invalid JSON for {sanitized.url}: {e}"
        )

    # Structural check — extraction key is required for downstream processing
    if "extraction" not in extraction:
        raise ExtractionError(
            f"Researcher output missing 'extraction' key for {sanitized.url}"
        )

    return extraction


# --- Stage 3: Validate & Cross-reference ---


def _build_analyst_prompt(
    extractions: list[ExtractionResult],
    schema: dict,
    topic: str,
    request_id: str,
    trust_level: str,
) -> str:
    """Build the user message for the analyst agent.

    The agent spec (analyst.md) is sent as the system prompt separately.
    This returns only the task portion — request context and extraction data.
    """
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


def _build_result_from_analyst(
    analyst_output: dict,
    extractions: list[ExtractionResult],
    schema: dict,
    request_id: str,
    topic: str,
    trust_level: str,
) -> dict:
    """Transform the analyst agent's output into the MCP return format."""
    validation = analyst_output.get("validation", {})
    consensus = validation.get("consensus_data", {})
    missing = validation.get("fields_missing", {})
    cross_report = validation.get("cross_source_report", {})
    anomaly_info = validation.get("anomaly_summary", {})

    # Extract flat data and field details from consensus
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

    # Overall confidence from cross-source agreement
    overall_agreement = cross_report.get("overall_agreement", 0.5)

    # Average field confidence as overall, weighted by agreement
    if field_details:
        field_confidences = [f["confidence"] for f in field_details.values()]
        overall_confidence = sum(field_confidences) / len(field_confidences)
    else:
        overall_confidence = 0.0

    # Determine status — analyst provides one, but we enforce trust level rules
    analyst_status = validation.get("status", "partial")
    analyst_recommendation = anomaly_info.get("recommendation", "review")

    source_count = len(extractions)
    all_schema_valid = all(ext.validation.valid for ext in extractions)

    critical_count = anomaly_info.get("critical_anomalies", 0)
    high_count = anomaly_info.get("high_anomalies", 0)

    if trust_level == "strict":
        if source_count < 3:
            status = "rejected"
        elif critical_count > 0 or analyst_recommendation == "reject":
            status = "rejected"
        elif high_count > 0 or not all_schema_valid or overall_agreement < 0.7:
            status = "partial"
        else:
            status = "validated"
    elif trust_level == "standard":
        if critical_count > 0 or analyst_recommendation == "reject":
            status = "rejected"
        elif high_count > 0 or not all_schema_valid:
            status = "partial"
        else:
            status = analyst_status
    else:  # exploratory
        if critical_count > 0:
            status = "rejected"
        elif not all_schema_valid:
            status = "partial"
        else:
            status = analyst_status

    # Check for missing required fields
    missing_field_names = list(missing.keys())
    missing_required = [
        fname for fname in missing_field_names
        if schema.get("fields", {}).get(fname, {}).get("required")
    ]
    if missing_required and status == "validated":
        status = "partial"

    # Build anomaly summary
    anomaly_patterns = anomaly_info.get("anomaly_patterns", [])
    total_anomalies = anomaly_info.get("total_anomalies_across_sources", 0)

    # Determine max severity
    max_severity = None
    if critical_count > 0:
        max_severity = "critical"
    elif high_count > 0:
        max_severity = "high"
    elif total_anomalies > 0:
        max_severity = "medium"

    # Build sources list
    sources = []
    for ext in extractions:
        sources.append({
            "url": ext.source.url,
            "fetch_date": ext.source.fetch_date,
            "sanitization_warnings": ext.source.sanitize_result.warnings,
        })

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
        "sources": sources,
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
    client = anthropic.AsyncAnthropic()

    agent_spec = (_PROJECT_DIR / "agents" / "analyst.md").read_text()
    user_message = _build_analyst_prompt(
        extractions, schema, topic, request_id, trust_level
    )

    response = await client.messages.create(
        model=ANALYST_MODEL,
        max_tokens=4096,
        system=agent_spec,
        messages=[{"role": "user", "content": user_message}],
    )

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    if not text.strip():
        raise ExtractionError("Analyst agent returned empty response")

    try:
        analyst_output = _parse_json_from_text(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ExtractionError(f"Analyst agent returned invalid JSON: {e}")

    if "validation" not in analyst_output:
        raise ExtractionError(
            "Analyst output missing 'validation' key"
        )

    return _build_result_from_analyst(
        analyst_output, extractions, schema, request_id, topic, trust_level
    )


# --- Main Orchestrator ---


async def execute_research(
    topic: str,
    schema: dict,
    trust_level: str = "standard",
    max_sources: int = 3,
    source_hints: list[str] | None = None,
    urls: list[str] | None = None,
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

    # Stage 1a: URL Discovery
    all_urls = list(urls or [])
    if len(all_urls) < max_sources:
        remaining = max_sources - len(all_urls)
        discovered = await discover_sources(
            topic=topic,
            source_hints=source_hints,
            max_sources=remaining,
        )
        existing = set(all_urls)
        for url in discovered:
            if url not in existing:
                all_urls.append(url)
                existing.add(url)

    if not all_urls:
        raise FetchError(
            "No URLs to fetch. Provide explicit URLs or a topic to search for."
        )

    # Stage 1b: Fetch & Sanitize
    sources = await fetch_and_sanitize(all_urls)

    if not sources:
        raise FetchError("All fetch attempts failed")

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
