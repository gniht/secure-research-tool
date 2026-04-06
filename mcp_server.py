#!/usr/bin/env python3
from __future__ import annotations

"""
Secure Research Tool — MCP Server.

Exposes a single `secure_research` tool that runs the full pipeline:
  1. Search (DuckDuckGo)
  2. Fetch & Sanitize (HTTP + HTML stripping + injection detection)
  3. Extract (isolated subagent via `claude -p`)
  4. Validate (schema validation + sanitizer floor)
  5. Cross-reference (isolated analyst subagent, if 2+ sources)
  6. Score & return

Also exposes `validate_research_schema` for pre-flight schema checks,
and the individual pipeline tools for manual/debugging use.

Security model:
  - AI stages run as isolated subagents (separate `claude -p` processes)
  - Subagents have no tools, no project context, no MCP access
  - Agent specs go in system prompts (trusted channel)
  - Untrusted web content goes in user messages only
  - Sanitizer floor: deterministic code cross-checks sanitizer findings
    against researcher's anomaly log
  - Analyst never sees raw web content
"""

import asyncio
import json
import logging

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
    parse_json_from_text,
)
from validation.schema_validator import validate_extraction as _validate_extraction
from subagent import run_researcher, run_analyst

logger = logging.getLogger(__name__)


mcp = FastMCP(
    "secure-research-tool",
    instructions=(
        "Secure pipeline for extracting structured data from untrusted web sources. "
        "Use `secure_research` for the full pipeline — it handles search, fetch, "
        "sanitize, extract (via isolated subagent), validate, cross-reference, "
        "and scoring automatically. Use `validate_research_schema` to check a "
        "schema before using it."
    ),
)


# --- Primary Tool: Full Pipeline ---


@mcp.tool(description="""\
Run the full secure research pipeline for a topic.

This is the primary tool. It handles the complete workflow:
1. Searches for relevant sources (DuckDuckGo)
2. Fetches and sanitizes web pages (injection pattern detection)
3. Extracts structured data via isolated AI subagent (researcher)
4. Validates extractions against schema + enforces sanitizer floor
5. Cross-references multiple sources via isolated AI subagent (analyst)
6. Builds scored result with confidence levels

Security: AI extraction and analysis run as isolated subagents with no
tools, no project context, and no ability to affect the calling system.

Args:
  topic: What to research (e.g., "Valheim inventory system")
  schema: Domain schema defining what data to extract (JSON object)
  trust_level: "strict" (3+ sources, any anomaly rejects),
               "standard" (2+ preferred, anomalies logged),
               "exploratory" (1 source OK, lower confidence)
  max_sources: Maximum number of sources to fetch (default 3)
  urls: Optional list of specific URLs to include. If fewer than
        max_sources, additional sources are searched.

Returns the full research result with data, confidence scores,
anomaly summary, and quality report.\
""")
async def secure_research(
    topic: str,
    schema: dict,
    trust_level: str = "standard",
    max_sources: int = 3,
    urls: list[str] | None = None,
) -> str:
    """Run the full secure research pipeline."""

    # --- Pre-flight: validate schema ---
    schema_result = validate_caller_schema(schema)
    if not schema_result.valid:
        return json.dumps({
            "error": "Schema validation failed",
            "errors": schema_result.errors,
            "warnings": schema_result.warnings,
        }, indent=2)

    # --- Stage 1a: Discover sources ---
    source_urls = list(urls or [])

    if len(source_urls) < max_sources:
        try:
            queries = _build_search_queries(topic, schema)
            seen_urls = set(source_urls)
            for query in queries:
                if len(source_urls) >= max_sources:
                    break
                results = execute_web_search(query, max_results=5)
                for r in results:
                    if r["url"] not in seen_urls and len(source_urls) < max_sources:
                        source_urls.append(r["url"])
                        seen_urls.add(r["url"])
        except Exception as e:
            if not source_urls:
                return json.dumps({
                    "error": f"Search failed and no URLs provided: {e}",
                }, indent=2)
            logger.warning(f"Search failed, proceeding with provided URLs: {e}")

    if not source_urls:
        return json.dumps({
            "error": "No sources found. Try different search terms or provide URLs directly.",
        }, indent=2)

    # --- Stage 1b: Fetch & Sanitize ---
    fetch_result = await _fetch_and_sanitize(source_urls)
    sources = fetch_result["sources"]
    fetch_errors = fetch_result["errors"]

    if not sources:
        return json.dumps({
            "error": "All fetches failed",
            "fetch_errors": fetch_errors,
        }, indent=2)

    # --- Stage 2: Extract (isolated subagents) ---
    extractions = []
    extraction_errors = []

    for source in sources:
        try:
            extraction = await run_researcher(
                sanitized_text=source["sanitized_text"],
                schema=schema,
                topic=topic,
            )
            extractions.append({
                "extraction": extraction,
                "source": source,
            })
        except Exception as e:
            logger.error(f"Extraction failed for {source['url']}: {e}")
            extraction_errors.append({
                "url": source["url"],
                "error": str(e),
            })

    if not extractions:
        return json.dumps({
            "error": "All extractions failed",
            "extraction_errors": extraction_errors,
            "fetch_errors": fetch_errors,
        }, indent=2)

    # --- Stage 3a: Validate each extraction ---
    validated = []
    for item in extractions:
        extraction = item["extraction"]
        source = item["source"]

        # Schema validation
        validation_result = _validate_extraction(extraction, schema)

        # Sanitizer floor enforcement
        extraction = _apply_sanitizer_floor(
            source.get("injection_patterns", []),
            extraction,
        )

        validated.append({
            "extraction": extraction,
            "validation": validation_result.to_dict(),
            "source_info": {
                "url": source["url"],
                "fetch_date": source["fetch_date"],
                "warnings": source.get("warnings", []),
            },
        })

    # --- Stage 3b: Cross-reference (if multiple sources) ---
    if len(validated) == 1:
        v = validated[0]
        result = _build_result_single(
            extraction=v["extraction"],
            validation_result=v["validation"],
            source_info=v["source_info"],
            schema=schema,
            topic=topic,
            trust_level=trust_level,
        )
    else:
        try:
            sanitized_extractions = [
                _sanitize_for_analyst(v["extraction"])
                for v in validated
            ]

            analyst_output = await run_analyst(
                sanitized_extractions=sanitized_extractions,
                schema=schema,
                topic=topic,
                trust_level=trust_level,
            )

            result = _build_result_multi(
                analyst_output=analyst_output,
                extractions=[v["extraction"] for v in validated],
                validation_results=[v["validation"] for v in validated],
                source_infos=[v["source_info"] for v in validated],
                schema=schema,
                topic=topic,
                trust_level=trust_level,
            )
        except Exception as e:
            logger.error(f"Analyst subagent failed: {e}")
            # Fall back to single-source result from best extraction
            v = validated[0]
            result = _build_result_single(
                extraction=v["extraction"],
                validation_result=v["validation"],
                source_info=v["source_info"],
                schema=schema,
                topic=topic,
                trust_level=trust_level,
            )
            result["_analyst_error"] = str(e)
            result["_fallback"] = "analyst_failed_using_first_source"

    # Attach metadata about the pipeline run
    result["_pipeline"] = {
        "sources_searched": len(source_urls),
        "sources_fetched": len(sources),
        "sources_extracted": len(extractions),
        "fetch_errors": fetch_errors,
        "extraction_errors": extraction_errors,
    }

    return json.dumps(result, indent=2)


def _build_search_queries(topic: str, schema: dict) -> list[str]:
    """Build search queries from the topic and schema fields."""
    queries = [topic]

    field_names = list(schema.get("fields", {}).keys())
    if field_names:
        key_fields = " ".join(field_names[:3])
        queries.append(f"{topic} {key_fields}")

    queries.append(f"{topic} wiki")

    domain = schema.get("domain", "")
    if domain:
        queries.append(f"{topic} {domain} details")

    return queries[:4]


# --- Schema Validation Tool ---


@mcp.tool(description="""\
Validate a domain schema before using it with secure_research.

Checks required keys, valid field types, range/enum definitions, etc.

Returns {valid: bool, errors: [...], warnings: [...]}.\
""")
def validate_research_schema(schema: dict) -> str:
    """Validate a domain schema."""
    try:
        result = validate_caller_schema(schema)
        return json.dumps({
            "valid": result.valid,
            "errors": result.errors,
            "warnings": result.warnings,
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "valid": False,
            "errors": [f"Schema validation failed: {e}"],
            "warnings": [],
        }, indent=2)


# --- Individual Pipeline Tools (for debugging/manual use) ---


@mcp.tool(description="""\
Search the web using DuckDuckGo. Returns search results.
Exposed for debugging — secure_research calls this internally.\
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
Fetch web pages and run the sanitization pipeline.
Exposed for debugging — secure_research calls this internally.\
""")
async def fetch_and_sanitize(urls: list[str]) -> str:
    """Fetch URLs and sanitize content."""
    result = await _fetch_and_sanitize(urls)
    return json.dumps(result, indent=2)


@mcp.tool(description="""\
Load an agent specification. Available: "researcher", "analyst", "search".
Exposed for reference — secure_research uses these internally via subagents.\
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
