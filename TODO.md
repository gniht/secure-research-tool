# TODO

## Pipeline Stubs to Implement

- [ ] **Stage 1: Web fetching** — `pipeline.py:fetch_and_sanitize()`
  - Web search mechanism (e.g., DuckDuckGo) to find relevant pages for a topic
  - Async HTTP client (httpx) to fetch pages
  - Wire fetched content through existing `sanitizer.py`
  - Return `SanitizedSource` objects with URL, fetch date, sanitized text

- [ ] **Stage 2: Researcher agent invocation** — `pipeline.py:extract_from_source()`
  - Call Claude API via Anthropic SDK with researcher prompt
  - Prompt construction already done (`_build_researcher_prompt()`)
  - Parse JSON response, handle malformed output
  - Enforce no-tools / no-web constraint via API parameters

- [ ] **Stage 3: Analyst cross-referencing** — `pipeline.py:validate_and_crossref()` (multi-source path)
  - Call Claude API with analyst prompt for 2+ extractions
  - Prompt construction already done (`_build_analyst_prompt()`)
  - Transform analyst output into the MCP return format
  - Single-source path already implemented

## Testing

- [ ] Unit tests for `validation/sanitizer.py`
- [ ] Unit tests for `validation/schema_validator.py`
- [ ] Unit tests for `pipeline.validate_caller_schema()`
- [ ] Integration test: end-to-end pipeline with known test data
- [ ] Behavioral audit test: verify Stage 3 rejects extractions missing the audit section
- [ ] Adversarial test: source text with injection attempts, verify anomaly logging

## Future

- [ ] Source reputation / quality scoring
- [ ] Batch mode for multiple topics
- [ ] Custom validation plugins for domain-specific plausibility checks
