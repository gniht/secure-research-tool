#!/usr/bin/env python3
from __future__ import annotations

"""
Secure Research Tool — MCP Server.

Exposes the deterministic stages of the research pipeline as MCP tools.
AI stages (extraction, cross-source analysis) are handled by the calling
agent via isolated subagents — this server provides the security
infrastructure around those AI calls.

Tools:
  search_web          — DuckDuckGo search, returns URLs
  fetch_and_sanitize  — HTTP fetch + sanitize + injection detection
  validate_schema     — Pre-flight schema check
  validate_extraction — Schema validation on extraction JSON
  apply_sanitizer_floor — Enforce severity floor on anomaly log
  sanitize_for_analyst  — Strip raw content from extraction for analyst
  build_result_single   — Confidence scoring for single-source result
  build_result_multi    — Confidence scoring for multi-source result
  get_agent_spec        — Load agent spec (researcher/analyst) for subagent prompts
"""

import json

from mcp.server.fastmcp import FastMCP

from pipeline import (
    execute_web_search,
    fetch_and_sanitize as _fetch_and_sanitize,
    validate_caller_schema,
    apply_sanitizer_floor as _apply_sanitizer_floor,
    sanitize_extraction_for_analyst as _sanitize_for_analyst,
    build_result_single as _build_result_single,
    build_result_multi as _build_result_multi,
    get_agent_spec as _get_agent_spec,
)
from validation.schema_validator import validate_extraction as _validate_extraction


mcp = FastMCP(
    "secure-research-tool",
    instructions=(
        "Secure pipeline for extracting structured data from untrusted web sources. "
        "This server provides the deterministic stages: search, fetch, sanitize, "
        "validate, and score. AI extraction and analysis are done by the calling "
        "agent via isolated subagents using the agent specs from get_agent_spec()."
    ),
)


# --- Tools ---


@mcp.tool(description="""\
Search the web using DuckDuckGo. Returns search results with title, URL, and snippet.

Use this to find relevant sources for a research topic. Run multiple queries \
to cover different angles. The calling agent decides what to search for — \
this tool just executes the search.

Returns a list of {title, url, snippet} objects per query.\
""")
def search_web(
    queries: list[str],
    max_results_per_query: int = 10,
) -> str:
    """Execute DuckDuckGo searches."""
    all_results = {}
    for query in queries:
        try:
            results = execute_web_search(query, max_results_per_query)
            all_results[query] = results
        except Exception as e:
            all_results[query] = {"error": str(e)}
    return json.dumps(all_results, indent=2)


@mcp.tool(description="""\
Fetch web pages and run the full sanitization pipeline on each.

Takes a list of URLs, fetches them in parallel, and returns sanitized text \
with injection pattern detection. The sanitized text is safe to pass to an \
extraction subagent.

Returns {sources: [...], errors: [...]} where each source has:
- url, fetch_date, sanitized_text
- injection_patterns: any prompt injection patterns detected (list)
- warnings: sanitization warnings
- original_length, sanitized_length, truncated

IMPORTANT: The sanitized_text may be very large. For the extraction step, \
pass each source's sanitized_text to an isolated subagent along with the \
schema and the researcher agent spec (from get_agent_spec('researcher')).\
""")
async def fetch_and_sanitize(urls: list[str]) -> str:
    """Fetch URLs and sanitize content."""
    result = await _fetch_and_sanitize(urls)
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Validate a domain schema before using it in the research pipeline.

Returns {valid: bool, errors: [...], warnings: [...]}.\
""")
def validate_schema(domain_schema: dict) -> str:
    """Validate a domain schema."""
    try:
        result = validate_caller_schema(domain_schema)
        return json.dumps({
            "valid": result.valid,
            "errors": result.errors,
            "warnings": result.warnings,
        }, indent=2)
    except Exception:
        return json.dumps({
            "valid": False,
            "errors": ["Schema validation failed unexpectedly"],
            "warnings": [],
        }, indent=2)


@mcp.tool(description="""\
Run deterministic schema validation on an extraction produced by the \
researcher subagent. Checks structural validity, field types, ranges, \
enum values, and required fields.

Args:
  extraction: the full JSON output from the researcher subagent
  schema: the domain schema used for the extraction

Returns {valid: bool, errors: [...], warnings: [...]}.\
""")
def validate_extraction(extraction: dict, schema: dict) -> str:
    """Validate extraction against schema."""
    result = _validate_extraction(extraction, schema)
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool(description="""\
Enforce a deterministic severity floor on the researcher's anomaly log \
based on injection patterns the sanitizer detected.

If the sanitizer found injection patterns but the researcher agent \
downplayed or omitted them, this adds entries to the anomaly log at \
the appropriate severity. This prevents a successful prompt injection \
from silencing its own detection.

Call this AFTER extraction, BEFORE building the final result.

Args:
  injection_patterns: the injection_patterns list from fetch_and_sanitize
  extraction: the full JSON output from the researcher subagent

Returns the extraction dict, possibly with additional anomaly_log entries.\
""")
def apply_sanitizer_floor(injection_patterns: list, extraction: dict) -> str:
    """Apply sanitizer floor to extraction."""
    result = _apply_sanitizer_floor(injection_patterns, extraction)
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Strip raw web content from an extraction before passing to the analyst \
subagent. The analyst should NEVER see raw source text — only structured \
field values and anomaly metadata.

Call this when preparing data for the analyst subagent (multi-source only).

Args:
  extraction: the full JSON output from the researcher subagent

Returns a sanitized version safe for the analyst to consume.\
""")
def sanitize_for_analyst(extraction: dict) -> str:
    """Sanitize extraction for analyst consumption."""
    result = _sanitize_for_analyst(extraction)
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Build the final scored result from a single-source extraction.

Use this when you have only one source. Applies confidence scoring, \
anomaly assessment, trust level rules, and status determination.

Args:
  extraction: the researcher subagent's full JSON output (after sanitizer floor)
  validation_result: output of validate_extraction as a dict
  source_info: {url, fetch_date, warnings} for the source
  schema: the domain schema
  topic: research topic string
  trust_level: "strict" | "standard" | "exploratory"

Returns the complete research result dict.\
""")
def build_result_single(
    extraction: dict,
    validation_result: dict,
    source_info: dict,
    schema: dict,
    topic: str,
    trust_level: str = "standard",
) -> str:
    """Build single-source result."""
    result = _build_result_single(
        extraction, validation_result, source_info,
        schema, topic, trust_level,
    )
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Build the final scored result from multi-source analyst output.

Use this when you have 2+ sources and have run the analyst subagent. \
Takes the analyst's consensus output and applies trust level rules, \
confidence scoring, and status determination.

Args:
  analyst_output: the analyst subagent's full JSON output
  extractions: list of researcher subagent outputs (one per source)
  validation_results: list of validate_extraction results (one per source)
  source_infos: list of {url, fetch_date, warnings} per source
  schema: the domain schema
  topic: research topic string
  trust_level: "strict" | "standard" | "exploratory"

Returns the complete research result dict.\
""")
def build_result_multi(
    analyst_output: dict,
    extractions: list[dict],
    validation_results: list[dict],
    source_infos: list[dict],
    schema: dict,
    topic: str,
    trust_level: str = "standard",
) -> str:
    """Build multi-source result."""
    result = _build_result_multi(
        analyst_output, extractions, validation_results,
        source_infos, schema, topic, trust_level,
    )
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Load an agent specification for use as a subagent system prompt.

Available agents:
- "researcher": Extraction agent — reads sanitized text, outputs structured JSON. \
Use as the system prompt for an isolated extraction subagent. The subagent should \
receive ONLY the agent spec + sanitized text + schema. No project context.
- "analyst": Cross-reference agent — compares multiple extractions, produces \
consensus. Use as the system prompt for an isolated analysis subagent. The subagent \
should receive ONLY the agent spec + sanitized extractions (from sanitize_for_analyst). \
Never raw web content.
- "search": Search strategy agent spec (reference only — search is handled by \
the calling agent directly).

Returns the full markdown specification text.\
""")
def get_agent_spec(agent_name: str) -> str:
    """Load an agent specification."""
    try:
        return _get_agent_spec(agent_name)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# --- Entry Point ---

if __name__ == "__main__":
    mcp.run()
