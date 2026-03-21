#!/usr/bin/env python3
"""
Secure Research Tool — MCP Server.

Exposes the research pipeline as MCP tools that AI agents can call.
"""

import json

from mcp.server.fastmcp import FastMCP

from pipeline import execute_research, validate_caller_schema


mcp = FastMCP(
    "secure-research-tool",
    description=(
        "Secure pipeline for extracting structured data from untrusted web sources. "
        "Handles fetching, sanitization, AI extraction, and multi-source validation "
        "with built-in prompt injection detection."
    ),
)


# --- Tool Descriptions ---
# These are the most important part of the interface — they teach calling agents
# how to use the tools.

SECURE_RESEARCH_DESCRIPTION = """\
Extract structured data from web sources with security isolation and multi-source validation.

This tool fetches web pages about a topic, sanitizes the content (stripping HTML, \
detecting injection patterns), extracts structured data using an isolated AI agent, \
and validates the results against your schema using cross-source comparison. \
The entire pipeline runs in isolation — no stage has both access to untrusted web \
content AND the ability to affect your project.

YOU PROVIDE:
- A topic to research (what to search for)
- A domain schema defining what data to extract (as a JSON object, NOT a file path)
- Optionally: urls, a list of specific URLs to fetch. If provided, these are used \
first. If fewer than max_sources, the tool searches for additional sources automatically. \
If you provide enough URLs to satisfy max_sources, no search is performed.
- Optionally: a trust level controlling validation strictness
- Optionally: max_sources to control how many web sources to fetch (1-10, default 3). \
More sources increase confidence but take longer.
- Optionally: source_hints, a list of keywords suggesting what kinds of sources to \
prefer when searching (e.g., ["wiki", "official_docs", "api_reference"]). Only used \
when the tool searches for sources — ignored if you provide all URLs explicitly.

SCHEMA FORMAT — you must provide a domain_schema object with these keys:
- "schema_id": unique identifier string (e.g., "product_specs_v2")
- "version": integer version number
- "domain": short domain name (e.g., "product_specs", "weather", "game_mechanics")
- "description": human-readable description of what this schema extracts
- "fields": object mapping field names to field definitions

Each field definition has:
- "type": one of "int", "float", "bool", "string", "enum"
- "description": what this field represents — be specific, the AI extraction agent \
reads this to know what to look for in web content
- "required": boolean, whether this field must be present for the result to be valid
- "range": [min, max] array — only for int/float, constrains valid values
- "max_length": integer — only for string, maximum character count
- "values": array of allowed strings — only for enum type

EXAMPLE SCHEMA:
{
  "schema_id": "inventory_v1",
  "version": 1,
  "domain": "inventory",
  "description": "Inventory system properties for a game",
  "fields": {
    "slot_count": {
      "type": "int",
      "range": [1, 500],
      "description": "Total number of inventory slots available to the player",
      "required": true
    },
    "has_weight_system": {
      "type": "bool",
      "description": "Whether the game uses an inventory weight/encumbrance system",
      "required": true
    },
    "stack_limit": {
      "type": "int",
      "range": [1, 9999],
      "description": "Maximum number of identical items that can stack in one slot",
      "required": false
    }
  }
}

TRUST LEVELS:
- "strict": requires 3+ independent sources, rejects on any high-severity anomaly. \
Use for production data or when accuracy is critical.
- "standard" (default): prefers 2+ sources, anomalies logged but data accepted if \
structurally valid. Good for normal research.
- "exploratory": 1 source acceptable, lower confidence scores. Use for initial \
discovery or when few sources exist.

WHAT YOU GET BACK:
- "status": "validated" (safe to use), "partial" (some concerns, review recommended), \
or "rejected" (data not trustworthy at the requested trust level)
- "data": the extracted field values matching your schema
- "confidence": overall confidence score 0.0-1.0
- "field_details": per-field confidence and source agreement info
- "quality_report": schema validity, cross-source agreement, anomaly counts
- "anomaly_summary": count and severity of detected issues (prompt injection attempts, \
contradictory data, suspicious formatting)

The tool handles all fetching, sanitization, AI extraction, and validation internally. \
You never see raw web content. Check "status" and "confidence" to decide whether to \
use the returned data.\
"""

VALIDATE_SCHEMA_DESCRIPTION = """\
Validate a domain schema before using it with secure_research.

Use this to check that your schema is structurally valid and will be accepted by the \
research pipeline. Returns validation errors and warnings. Useful when constructing \
schemas dynamically.

The schema format is the same as the "domain_schema" parameter in secure_research — \
see that tool's description for the full format specification.\
"""


# --- Tools ---


@mcp.tool(description=SECURE_RESEARCH_DESCRIPTION)
async def secure_research(
    topic: str,
    domain_schema: dict,
    trust_level: str = "standard",
    max_sources: int = 3,
    source_hints: list[str] | None = None,
    urls: list[str] | None = None,
) -> str:
    """Execute the secure research pipeline."""
    result = await execute_research(
        topic=topic,
        schema=domain_schema,
        trust_level=trust_level,
        max_sources=max_sources,
        source_hints=source_hints,
        urls=urls,
    )
    return json.dumps(result, indent=2)


@mcp.tool(description=VALIDATE_SCHEMA_DESCRIPTION)
async def validate_research_schema(domain_schema: dict) -> str:
    """Validate a domain schema for use with secure_research."""
    try:
        result = validate_caller_schema(domain_schema)
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


# --- Entry Point ---

if __name__ == "__main__":
    mcp.run()
