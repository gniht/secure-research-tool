#!/usr/bin/env python3
"""
Secure Research Tool — CLI entry point.

Human-facing interface to the research pipeline.
For AI agent access, use the MCP server (mcp_server.py) instead.

Usage:
  python cli.py research --topic "Valheim inventory" --schema schemas/example_schema.json
  python cli.py sanitize --input raw_page.html
  python cli.py validate --extraction staging/extracted/result.json --schema schemas/example_schema.json
  python cli.py check-schema --schema schemas/my_schema.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from validation.schema_validator import load_schema, validate_extraction
from validation.sanitizer import sanitize
from pipeline import execute_research, validate_caller_schema, SchemaError


STAGING_DIR = Path("staging")
RAW_DIR = STAGING_DIR / "raw"
EXTRACTED_DIR = STAGING_DIR / "extracted"
VALIDATED_DIR = STAGING_DIR / "validated"


def ensure_dirs():
    """Create staging directories if they don't exist."""
    for d in (RAW_DIR, EXTRACTED_DIR, VALIDATED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cmd_research(args):
    """Handle the 'research' command — run the full pipeline."""
    ensure_dirs()

    schema = load_schema(Path(args.schema))

    print(f"Starting research pipeline...")
    print(f"  Topic: {args.topic}")
    print(f"  Domain: {schema['domain']}")
    print(f"  Schema: {schema['schema_id']}")
    print(f"  Trust level: {args.trust_level}")
    print(f"  Max sources: {args.max_sources}")
    print()

    try:
        result = asyncio.run(execute_research(
            topic=args.topic,
            schema=schema,
            trust_level=args.trust_level,
            max_sources=args.max_sources,
        ))
    except SchemaError as e:
        print(f"Schema error: {e}")
        sys.exit(1)
    except NotImplementedError as e:
        print(f"Not yet implemented: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Pipeline error: {e}")
        sys.exit(1)

    # Save result
    output_path = VALIDATED_DIR / f"result_{result['request_id'][:8]}.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Status: {result['status']}")
    print(f"Confidence: {result['confidence']}")
    print(f"Sources: {result['source_count']}")
    print(f"Result saved: {output_path}")

    if result.get("anomaly_summary", {}).get("total", 0) > 0:
        print(f"Anomalies: {result['anomaly_summary']['total']}")

    sys.exit(0 if result["status"] == "validated" else 1)


def cmd_sanitize(args):
    """Handle the 'sanitize' command — clean raw HTML/text content."""
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}")
        sys.exit(1)

    ensure_dirs()

    with open(input_path) as f:
        raw_content = f.read()

    result = sanitize(raw_content)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = RAW_DIR / f"sanitized_{input_path.stem}.txt"

    with open(output_path, "w") as f:
        f.write(result.text)

    report_path = output_path.with_suffix(".report.json")
    with open(report_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)

    print(f"Sanitized: {input_path} -> {output_path}")
    print(f"  Original: {result.original_length} chars")
    print(f"  Sanitized: {result.sanitized_length} chars")
    if result.truncated:
        print("  WARNING: Content was truncated")
    if result.warnings:
        for w in result.warnings:
            print(f"  WARNING: {w}")


def cmd_validate(args):
    """Handle the 'validate' command — validate extraction against schema."""
    extraction_path = Path(args.extraction)
    schema_path = Path(args.schema)

    if not extraction_path.exists():
        print(f"Error: extraction file not found: {extraction_path}")
        sys.exit(1)

    if not schema_path.exists():
        print(f"Error: schema file not found: {schema_path}")
        sys.exit(1)

    schema = load_schema(schema_path)

    with open(extraction_path) as f:
        extraction = json.load(f)

    result = validate_extraction(extraction, schema)

    output_path = VALIDATED_DIR / f"validation_{extraction_path.stem}.json"
    ensure_dirs()
    with open(output_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)

    print(f"Validation: {'PASSED' if result.valid else 'FAILED'}")
    if result.errors:
        print("Errors:")
        for e in result.errors:
            print(f"  - {e}")
    if result.warnings:
        print("Warnings:")
        for w in result.warnings:
            print(f"  - {w}")

    sys.exit(0 if result.valid else 1)


def cmd_check_schema(args):
    """Handle the 'check-schema' command — validate a domain schema."""
    schema_path = Path(args.schema)
    if not schema_path.exists():
        print(f"Error: schema file not found: {schema_path}")
        sys.exit(1)

    with open(schema_path) as f:
        schema = json.load(f)

    result = validate_caller_schema(schema)

    print(f"Schema: {'VALID' if result.valid else 'INVALID'}")
    if result.errors:
        print("Errors:")
        for e in result.errors:
            print(f"  - {e}")
    if result.warnings:
        print("Warnings:")
        for w in result.warnings:
            print(f"  - {w}")

    sys.exit(0 if result.valid else 1)


def main():
    parser = argparse.ArgumentParser(
        description="Secure Research Tool — structured data extraction from untrusted web sources"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # research command
    research_parser = subparsers.add_parser("research", help="Run the full research pipeline")
    research_parser.add_argument("--topic", required=True, help="Research topic")
    research_parser.add_argument("--schema", required=True, help="Path to domain schema JSON")
    research_parser.add_argument("--trust-level", default="standard",
                                 choices=["strict", "standard", "exploratory"],
                                 help="Trust level for validation strictness")
    research_parser.add_argument("--max-sources", type=int, default=3,
                                 help="Maximum number of sources to fetch")

    # sanitize command
    sanitize_parser = subparsers.add_parser("sanitize", help="Sanitize raw HTML/text content")
    sanitize_parser.add_argument("--input", required=True, help="Path to raw content file")
    sanitize_parser.add_argument("--output", help="Output path (default: staging/raw/sanitized_*.txt)")

    # validate command
    validate_parser = subparsers.add_parser("validate", help="Validate extraction against schema")
    validate_parser.add_argument("--extraction", required=True, help="Path to extraction JSON")
    validate_parser.add_argument("--schema", required=True, help="Path to domain schema JSON")

    # check-schema command
    check_schema_parser = subparsers.add_parser("check-schema", help="Validate a domain schema")
    check_schema_parser.add_argument("--schema", required=True, help="Path to domain schema JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "research": cmd_research,
        "sanitize": cmd_sanitize,
        "validate": cmd_validate,
        "check-schema": cmd_check_schema,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
