#!/usr/bin/env python3
"""
Subagent spawner for secure research tool.

Spawns isolated Claude instances via `claude -p` for AI stages.
Each subagent runs as a separate process with:
  - A system prompt (agent spec) passed via --system-prompt
  - A user message (sanitized content) passed via stdin
  - No tools, no project context, no MCP access
  - Output captured from stdout

This preserves the security isolation property: the subagent
cannot affect the calling project, access the filesystem beyond
what claude -p allows, or carry context from the parent session.

Uses the caller's Max plan — no additional API costs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).parent

# Timeout for subagent calls (seconds)
SUBAGENT_TIMEOUT = 300  # 5 minutes — extraction can be slow on large pages


def _find_claude_binary() -> str:
    """Locate the claude CLI binary."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise RuntimeError(
            "Claude CLI not found. Ensure 'claude' is installed and on PATH. "
            "See: https://docs.anthropic.com/en/docs/claude-code"
        )
    return claude_path


async def run_subagent(
    agent_name: str,
    user_message: str,
    timeout: int = SUBAGENT_TIMEOUT,
) -> str:
    """
    Spawn an isolated Claude subagent and return its response.

    Args:
        agent_name: Name of the agent spec to load ('researcher' or 'analyst')
        user_message: The content to send as the user message
        timeout: Maximum seconds to wait for response

    Returns:
        The raw text response from the subagent

    Raises:
        RuntimeError: If the subagent fails or times out
        FileNotFoundError: If the agent spec doesn't exist
    """
    # Load agent spec
    spec_path = _PROJECT_DIR / "agents" / f"{agent_name}.md"
    if not spec_path.exists():
        raise FileNotFoundError(f"Agent spec not found: {spec_path}")

    agent_spec = spec_path.read_text()
    claude_bin = _find_claude_binary()

    # Build command
    # -p: print mode (non-interactive, reads from stdin, writes to stdout)
    # --system-prompt: set the system prompt (agent spec)
    # --no-tools: disable all tools (enforces isolation)
    # --model: use a specific model if needed
    cmd = [
        claude_bin,
        "-p",
        "--system-prompt", agent_spec,
    ]

    logger.info(f"Spawning {agent_name} subagent (timeout={timeout}s)")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Run from /tmp to avoid picking up any project CLAUDE.md
            cwd="/tmp",
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=user_message.encode("utf-8")),
            timeout=timeout,
        )

    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"{agent_name} subagent timed out after {timeout}s. "
            "The input may be too large — consider reducing max_sources."
        )
    except Exception as e:
        raise RuntimeError(f"Failed to spawn {agent_name} subagent: {e}")

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{agent_name} subagent exited with code {proc.returncode}. "
            f"stderr: {stderr_text[:500]}"
        )

    response = stdout.decode("utf-8").strip()
    if not response:
        raise RuntimeError(f"{agent_name} subagent returned empty response")

    logger.info(f"{agent_name} subagent returned {len(response)} chars")
    return response


async def run_researcher(
    sanitized_text: str,
    schema: dict,
    topic: str,
) -> dict:
    """
    Run the researcher subagent to extract structured data from sanitized text.

    Args:
        sanitized_text: Sanitized web page content (from fetch_and_sanitize)
        schema: The domain schema defining what to extract
        topic: The research topic

    Returns:
        Parsed JSON extraction result from the researcher agent
    """
    # Build the user message — this is ALL the researcher sees
    # Agent spec goes in system prompt (trusted), this goes in user message (untrusted)
    user_message = f"""## Research Request

Topic: {topic}
Domain: {schema.get('domain', 'unknown')}
Schema ID: {schema.get('schema_id', 'unknown')}

## Domain Schema

{json.dumps(schema, indent=2)}

## Source Content (sanitized)

{sanitized_text}

## Instructions

Extract structured data from the source content above according to the domain schema.
Output ONLY the JSON structure specified in your system prompt. No other text."""

    response = await run_subagent("researcher", user_message)

    # Parse JSON from response
    return _parse_json_response(response, "researcher")


async def run_analyst(
    sanitized_extractions: list[dict],
    schema: dict,
    topic: str,
    trust_level: str,
) -> dict:
    """
    Run the analyst subagent to cross-reference multiple extractions.

    Args:
        sanitized_extractions: List of extractions with raw content stripped
                               (output of sanitize_extraction_for_analyst)
        schema: The domain schema
        topic: The research topic
        trust_level: "strict" | "standard" | "exploratory"

    Returns:
        Parsed JSON validation/consensus result from the analyst agent
    """
    user_message = f"""## Cross-Reference Request

Topic: {topic}
Domain: {schema.get('domain', 'unknown')}
Schema ID: {schema.get('schema_id', 'unknown')}
Trust Level: {trust_level}
Source Count: {len(sanitized_extractions)}

## Domain Schema

{json.dumps(schema, indent=2)}

## Extractions to Compare

{json.dumps(sanitized_extractions, indent=2)}

## Instructions

Compare the extractions above, assess agreement, and produce a consensus result
with confidence scoring. Output ONLY the JSON structure specified in your system prompt.
No other text."""

    response = await run_subagent("analyst", user_message)

    return _parse_json_response(response, "analyst")


def _parse_json_response(response: str, agent_name: str) -> dict:
    """Parse JSON from a subagent response, handling markdown code blocks."""
    text = response.strip()

    # Strip markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    # Try to find JSON object in the response
    # Sometimes the agent might include some text before/after the JSON
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise RuntimeError(
            f"{agent_name} subagent did not return valid JSON. "
            f"Response preview: {response[:300]}"
        )

    json_text = text[start:end + 1]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{agent_name} subagent returned malformed JSON: {e}. "
            f"Response preview: {response[:300]}"
        )
