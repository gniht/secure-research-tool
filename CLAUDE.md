# Secure Research Tool

General-purpose pipeline for AI-driven extraction of structured data from untrusted web sources, with built-in security isolation, anomaly detection, and multi-stage validation.

**This tool is domain-agnostic.** Callers provide domain schemas defining what to extract. The tool handles fetching, sanitization, extraction, anomaly detection, and validation. It knows nothing about the caller's domain — it just extracts structured data safely.

## Architecture

Three-stage pipeline with strict isolation between stages:

1. **Stage 1: Fetch & Sanitize** (no AI) — fetches web content, strips HTML, sanitizes text, saves to `staging/raw/`
2. **Stage 2: Extract** (isolated AI) — researcher agent reads sanitized text, extracts structured data against the caller-provided schema, logs anomalies. Writes to `staging/extracted/`
3. **Stage 3: Validate** (no AI + analyst AI) — schema validation (code), then analyst agent cross-references multiple extractions. Writes to `staging/validated/`

**Key security property:** No single stage has both access to untrusted web content AND the ability to affect the calling project.

## Project Structure

```
secure-research-tool/
  agents/
    researcher.md          # Stage 2 agent specification
    analyst.md             # Stage 3 analyst agent specification
  schemas/
    example_schema.json    # Example showing the schema format callers should follow
  validation/
    schema_validator.py    # Structural validation (no AI)
    sanitizer.py           # HTML stripping + injection pattern detection
  staging/                 # Working directories (gitignored)
    raw/                   # Stage 1 output: sanitized text + fetch metadata
    extracted/             # Stage 2 output: structured JSON extractions
    validated/             # Stage 3 output: validated, cross-referenced results
  docs/
    design.md              # Full design document
  cli.py                   # CLI entry point
```

## Usage

Callers provide their own domain schemas. The `--schema` parameter points to a JSON file the caller creates for their specific use case.

```bash
# Create a research request (caller provides their schema)
python cli.py research --topic "Valheim inventory system" --domain inventory --schema /path/to/callers/inventory_schema.json

# Sanitize raw fetched content
python cli.py sanitize --input raw_page.html

# Validate an extraction against the caller's schema
python cli.py validate --extraction staging/extracted/result.json --schema /path/to/callers/inventory_schema.json
```

## Schema Format

Callers define schemas as JSON files. See `schemas/example_schema.json` for the format. Required keys:

- `schema_id` — unique identifier for this schema version
- `version` — integer version number
- `domain` — domain name (caller-defined, e.g., "inventory", "weather_data", "product_specs")
- `description` — human-readable description
- `fields` — dict of field definitions, each with:
  - `type` — one of: `int`, `float`, `bool`, `string`, `enum`
  - `description` — what this field represents (used by the extraction agent)
  - `required` — whether the field must be present for validation to pass
  - `range` — (numeric types) `[min, max]` for validation
  - `max_length` — (string type) maximum character count
  - `values` — (enum type) list of allowed values

The schema serves dual purpose: it tells the extraction agent what to look for, and it tells the validator what to accept.

## Agent Specifications

Agent prompts are in `agents/`. These are designed to be used with Claude (or compatible AI):
- **researcher.md** — extraction agent that reads sanitized text and produces structured JSON with anomaly logging
- **analyst.md** — validation agent that cross-references multiple extractions and produces consensus results

The agents are intentionally constrained:
- Researcher: can only read text and output JSON. No shell, no web, no project access.
- Analyst: can only read extraction JSON. Never sees raw web content.

## Development Guidelines

- **Domain-agnostic** — this tool knows nothing about specific use cases. All domain knowledge comes from caller-provided schemas.
- **Schemas are caller-provided** — the `schemas/` directory contains only the example format. Callers maintain their own schemas in their own projects.
- **No AI in validation code** — `validation/` contains only deterministic Python
- **Staging is ephemeral** — `staging/` contents are working data, not versioned
- **Agent specs are versioned** — changes to agent prompts should be tracked in git
- **Stateless per invocation** — the tool doesn't remember previous runs
