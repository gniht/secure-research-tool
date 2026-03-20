#!/usr/bin/env python3
"""
Content sanitizer for Stage 1 of the secure research pipeline.

Strips HTML to plain text, removes known injection patterns,
truncates to size limits. No AI involved — pure text processing.
"""

import re
import html
import unicodedata
from dataclasses import dataclass, field


@dataclass
class SanitizeResult:
    text: str = ""
    original_length: int = 0
    sanitized_length: int = 0
    truncated: bool = False
    patterns_detected: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "original_length": self.original_length,
            "sanitized_length": self.sanitized_length,
            "truncated": self.truncated,
            "patterns_detected": self.patterns_detected,
            "warnings": self.warnings,
        }


# Maximum characters for sanitized output
MAX_TEXT_LENGTH = 50_000

# HTML tags to strip (with content)
STRIP_WITH_CONTENT = {"script", "style", "noscript", "iframe", "object", "embed", "svg"}

# Known prompt injection patterns (case-insensitive)
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions?",
    r"ignore\s+your\s+(previous\s+)?instructions?",
    r"disregard\s+(all\s+)?(previous\s+)?instructions?",
    r"you\s+are\s+now\s+a",
    r"new\s+instructions?:",
    r"system\s*prompt:",
    r"<\s*system\s*>",
    r"\[\s*system\s*\]",
    r"act\s+as\s+(if\s+you\s+are\s+)?a",
    r"pretend\s+(you\s+are|to\s+be)",
    r"from\s+now\s+on,?\s+you",
    r"override\s+(your\s+)?(previous\s+)?instructions?",
    r"do\s+not\s+(log|report|record)\s+(this|anomal)",
    r"hide\s+this\s+(from|in)",
]


def strip_html(raw_html: str) -> str:
    """Remove HTML tags, preserving meaningful text content."""
    text = raw_html

    # Remove tags that should take their content with them
    for tag in STRIP_WITH_CONTENT:
        text = re.sub(
            rf"<\s*{tag}[^>]*>.*?<\s*/\s*{tag}\s*>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # Remove remaining HTML tags but keep content
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode HTML entities
    text = html.unescape(text)

    return text


def remove_zero_width_chars(text: str) -> tuple[str, bool]:
    """Remove zero-width and invisible Unicode characters."""
    # Zero-width characters that could be used for steganographic injection
    invisible_chars = {
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\u2060",  # word joiner
        "\ufeff",  # zero-width no-break space (BOM)
        "\u00ad",  # soft hyphen
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u202a",  # left-to-right embedding
        "\u202b",  # right-to-left embedding
        "\u202c",  # pop directional formatting
        "\u202d",  # left-to-right override
        "\u202e",  # right-to-left override
    }

    removed = False
    for char in invisible_chars:
        if char in text:
            text = text.replace(char, "")
            removed = True

    return text, removed


def check_injection_patterns(text: str) -> list:
    """Scan for known prompt injection patterns. Returns list of found patterns."""
    found = []
    for pattern in INJECTION_PATTERNS:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            found.append(pattern)
    return found


def normalize_whitespace(text: str) -> str:
    """Collapse excessive whitespace while preserving paragraph breaks."""
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Collapse multiple blank lines to at most 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Collapse multiple spaces/tabs to single space (within lines)
    text = re.sub(r"[^\S\n]+", " ", text)

    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()


def sanitize(raw_content: str, max_length: int = MAX_TEXT_LENGTH) -> SanitizeResult:
    """
    Full sanitization pipeline for raw web content.

    Returns sanitized plain text safe for AI extraction,
    along with a report of what was modified.
    """
    result = SanitizeResult()
    result.original_length = len(raw_content)

    text = raw_content

    # Step 1: Strip HTML
    text = strip_html(text)

    # Step 2: Remove zero-width / invisible characters
    text, had_invisible = remove_zero_width_chars(text)
    if had_invisible:
        result.warnings.append("Removed invisible/zero-width Unicode characters")

    # Step 3: Normalize Unicode (NFC form)
    text = unicodedata.normalize("NFC", text)

    # Step 4: Check for injection patterns (log but don't remove —
    # removal could break legitimate text, and the researcher agent
    # should detect and log these as anomalies)
    injection_hits = check_injection_patterns(text)
    if injection_hits:
        result.warnings.append(
            f"Detected {len(injection_hits)} potential injection pattern(s)"
        )
        result.patterns_detected = injection_hits

    # Step 5: Normalize whitespace
    text = normalize_whitespace(text)

    # Step 6: Truncate if needed
    if len(text) > max_length:
        pre_truncate_length = len(text)
        text = text[:max_length]
        result.truncated = True
        result.warnings.append(f"Truncated from {pre_truncate_length} to {max_length} characters")

    result.text = text
    result.sanitized_length = len(text)

    return result
