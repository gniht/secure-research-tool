# TODO

## Pipeline Stubs to Implement

- [x] **Stage 1a: URL discovery** — `pipeline.py:discover_sources()`
  - Search agent (Sonnet) constructs queries and evaluates results
  - DuckDuckGo search via tool use
  - Agent spec: `agents/search.md`

- [x] **Stage 1b: Fetch & sanitize** — `pipeline.py:fetch_and_sanitize()`
  - Async HTTP fetching with httpx
  - Wired through existing `sanitizer.py`
  - User-provided URLs supported via `--url` flag / `urls` parameter

- [ ] **Stage 2: Researcher agent invocation** — `pipeline.py:extract_from_source()`
  - Call Claude API (Opus) with researcher prompt
  - Prompt construction already done (`_build_researcher_prompt()`)
  - Parse JSON response, handle malformed output
  - Enforce no-tools / no-web constraint via API parameters

- [ ] **Stage 3: Analyst cross-referencing** — `pipeline.py:validate_and_crossref()` (multi-source path)
  - Call Claude API (Opus) with analyst prompt for 2+ extractions
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
