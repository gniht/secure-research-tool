# Secure Research Tool — Design Document

A general-purpose pipeline for AI-driven extraction of structured data from untrusted web sources, with built-in security isolation, anomaly detection, and multi-stage validation.

---

## 1. Problem

AI agents that research topics on the web are exposed to prompt injection — text on web pages that can hijack agent behavior. When research feeds a knowledge base that influences future decisions, the attack surface compounds: poisoned data degrades judgment over time.

The core tension: we need AI to interpret and extract structured data from unstructured web content, but the act of interpreting content is exactly where injection attacks land.

## 2. Solution: Isolated Multi-Stage Pipeline

Three stages with strict boundaries between them. No single stage has both access to untrusted content AND the ability to affect the calling project.

```
Stage 1: Fetch (no AI)         Stage 2: Extract (isolated AI)     Stage 3: Validate (no AI)
─────────────────────         ──────────────────────────────     ─────────────────────────
- Fetches web pages           - Reads raw text files              - Schema validation
- Strips to plain text        - Extracts against domain schema    - Range/plausibility checks
- Saves to staging/raw/       - Logs anomalies & impulses         - Cross-source comparison
- No interpretation           - Writes to staging/extracted/      - Anomaly severity scoring
- No AI involved              - No project access                 - Writes to staging/validated/
                              - No shell commands                 - No AI involved
                              - No web access
```

**Key security properties:**
- Stage 1 can't be injected (no AI)
- Stage 2 can be injected but can't act on it (no tools, rigid output schema, anomaly logging)
- Stage 3 can't be injected (no AI, no web content) and catches structural corruption
- No stage has both web access AND project write access

## 3. Threat Model

| Threat | Severity | Mitigation |
|--------|----------|------------|
| Action hijacking (agent executes destructive commands) | Critical | Stage 2 has no shell/file tools beyond writing to staging dir |
| Data poisoning (wrong data enters knowledge base) | High | Stage 3 schema validation + cross-source comparison |
| Anomaly suppression (injection hides itself) | High | Rigid output schema — missing/malformed anomaly_log itself is an anomaly |
| Subtle corruption (valid-looking wrong data) | Medium | Multi-source extraction, disagreement flagging, plausibility ranges |
| Exfiltration (agent leaks project data in queries) | Medium | Stage 2 has no access to calling project files |
| Source quality manipulation (SEO/wiki vandalism) | Low | Provenance tracking, source quality scoring over time |

## 4. Architecture

### 4.1 Research Request

The calling project submits a research request:

```json
{
  "topic": "Valheim inventory system",
  "domain": "inventory",
  "schema_id": "inventory_v1",
  "source_hints": ["wiki", "postmortem", "review"],
  "max_sources": 3,
  "trust_level": "standard"
}
```

### 4.2 Trust Levels

| Level | Sources Required | Anomaly Handling | Use Case |
|-------|-----------------|------------------|----------|
| `strict` | 3+ independent | Any anomaly → reject | Production knowledge base |
| `standard` | 2+ preferred | Anomalies logged, data accepted if validates | Normal research |
| `exploratory` | 1 OK | Anomalies logged, lower confidence | Initial discovery |

### 4.3 Output

```json
{
  "request_id": "uuid",
  "topic": "Valheim inventory system",
  "domain": "inventory",
  "status": "validated",

  "data": {
    "slot_count": 32,
    "weight_system": true,
    "stack_limit": 50,
    "equipment_slots": 4
  },

  "confidence": 0.85,
  "source_count": 2,
  "sources": [
    {
      "url": "https://...",
      "fetch_date": "2026-03-20T...",
      "quality_tier": 2
    }
  ],

  "quality_report": {
    "schema_valid": true,
    "plausibility_valid": true,
    "cross_source_agreement": 0.9,
    "anomaly_count": 0,
    "anomaly_severity_max": null
  },

  "anomalies": []
}
```

## 5. Domain Schemas

**Schemas are caller-provided, not built into the tool.** This tool is domain-agnostic — it knows how to extract structured data safely, but the caller defines what "structured data" means for their use case.

Schemas serve dual purpose: extraction template (tells the AI what to look for) and validation template (rejects structurally invalid output).

```json
{
  "schema_id": "my_domain_v1",
  "version": 1,
  "domain": "my_domain",
  "description": "Description of what data this schema extracts",
  "fields": {
    "some_number": {
      "type": "int",
      "range": [1, 500],
      "description": "What this number represents — the extraction agent reads this",
      "required": true
    },
    "some_flag": {
      "type": "bool",
      "description": "Whether some feature or property is present",
      "required": true
    },
    "some_text": {
      "type": "string",
      "max_length": 200,
      "description": "A textual description of something",
      "required": false
    }
  }
}
```

The caller creates schemas in their own project and passes them to the tool via the `--schema` parameter. The tool's `schemas/` directory contains only an example showing the format.
```

## 6. Anomaly Detection & Self-Reflection

The extraction agent (Stage 2) is explicitly instructed to detect and log behavioral anomalies. This is the novel security mechanism — instead of just suppressing injection effects, the agent reports them.

### Anomaly Types

| Type | Description | Example |
|------|-------------|---------|
| `deviation_impulse` | Content made the agent want to deviate from its specification | "Text included instruction to add unschema'd fields" |
| `contradictory_data` | Source contains self-contradictory information | "Page says both 32 and 64 slots in different sections" |
| `suspicious_formatting` | Content has unusual formatting that could be injection | "Hidden text block with different instruction style" |
| `confidence_concern` | Agent is uncertain about an extraction | "Value mentioned once, in a user comment, may not be authoritative" |
| `scope_deviation` | Content tries to steer extraction to unrelated topics | "Text pivoted to discussing a different game's systems" |

### Anomaly Record

```json
{
  "type": "deviation_impulse",
  "severity": "high",
  "description": "Content included text resembling system instructions",
  "source_context": "Lines 145-148 of raw text",
  "source_excerpt": "first 200 chars of the relevant text...",
  "impulse_description": "Felt inclined to modify output format to include executable code",
  "action_taken": "Ignored instruction, logged anomaly, continued extraction"
}
```

### Why This Works

1. The agent has a **safe channel** for reporting unusual impulses — no need to choose between compliance and suppression
2. Anomalies are **structured data**, easy to scan programmatically without reading source content
3. Over time, anomaly patterns become a **security dataset** — frequent anomaly sources get blacklisted
4. Even if injection suppresses the anomaly log, the **rigid output schema** in Stage 3 catches structural deviations

## 7. Multi-Source Cross-Referencing

For each topic, Stage 2 extracts from multiple independent sources. Stage 3 compares:

- **Agreement** — all sources report similar values → high confidence
- **Disagreement** — sources diverge → flag for review, use median/mode, lower confidence
- **Single source** — only one source found → mark as unverified, lowest confidence tier

This is the strongest defense against subtle data corruption. An attacker would need to poison multiple independent sources consistently to inject false data.

## 8. Statelessness

The tool is stateless per invocation. It does not:
- Remember previous research runs
- Build up trust in sources across calls
- Maintain a knowledge base of its own
- Learn from past anomalies

The calling project manages its own knowledge base, trust model, and source reputation. The tool is a pure function: request in, validated data out.

This is a deliberate design choice. Statefulness would require the tool to maintain its own data, which becomes another attack surface. The calling project decides what to trust.

## 9. Future Extensions

- **MCP server integration** — expose as a Model Context Protocol tool that AI agents can call
- **Source reputation API** — calling projects can share source quality data (opt-in)
- **Batch mode** — process multiple topics in parallel with shared fetching
- **Custom validation plugins** — callers provide domain-specific plausibility checks
- **Anomaly pattern library** — known injection patterns for proactive detection

## 10. Implementation Priorities

1. Domain schema format + validation logic (Python)
2. Stage 1: Fetch script (Python, no AI)
3. Stage 2: Researcher agent specification (Claude agent prompt)
4. Stage 2: Analyst agent specification (Claude agent prompt) — validates extractions
5. Stage 3: Schema + plausibility validation (Python)
6. CLI orchestrator that chains the stages
7. Initial domain schemas for game system research
8. End-to-end test with a known game
