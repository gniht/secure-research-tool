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

- [x] **Stage 2: Researcher agent invocation** — `pipeline.py:extract_from_source()`
  - Call Claude API (Opus) with researcher prompt
  - Agent spec as system prompt, source text in user message (security boundary)
  - Parse JSON response, handle malformed output
  - No tools passed — enforces isolation constraint

- [x] **Stage 3: Analyst cross-referencing** — `pipeline.py:validate_and_crossref()` (multi-source path)
  - Call Claude API (Opus) with analyst prompt for 2+ extractions
  - Agent spec as system prompt, extraction data in user message
  - `_build_result_from_analyst()` transforms output into MCP return format
  - Trust level enforcement applied on top of analyst's recommendation

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
