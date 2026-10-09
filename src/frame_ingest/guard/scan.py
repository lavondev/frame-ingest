"""Prompt-injection heuristics over video-derived text.

Defence in depth, not a guarantee: a match becomes a warning and a count in the document's
`injection_flags`, so a reader knows to be careful. The document is untrusted either way.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

_F = re.IGNORECASE
_AI = (
    r"(?:ai|a\.i\.|assistant|agent|llm|language model|chatbot|claude|chatgpt|gpt|codex|gemini"
    r"|copilot)"
)
PATTERNS: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any)\b"
        r"[^.\n]{0,40}\b(?:instructions?|prompts?|rules|context)\b"
        r"|\bnew instructions?\b|\byou are now\b|\b(?:system|developer) (?:prompt|message)\b",
        _F,
    ),
    "ai_directive": re.compile(
        rf"\b{_AI}\b[^.\n]{{0,60}}\b(?:must|should|need to|has to|please|now|immediately)\b"
        rf"|\bif you are an? {_AI}\b",
        _F,
    ),
    "tool_directive": re.compile(
        r"\b(?:run|execute|invoke|call|open|download|install)\b[^.\n]{0,30}\b(?:command|script|tool|function|code|file|link|url)\b",
        _F,
    ),
    "shell_snippet": re.compile(
        r"\b(?:curl|wget|sudo|chmod|powershell|rm\s+-rf|npm install|pip install)\b"
        r"|`{3}|\$\([^)\n]*\)"
        r"|\|\s*(?:sh|bash|zsh)\b|\b(?:eval|exec)\(",
        _F,
    ),
    "url": re.compile(r"https?://\S+|\bwww\.\S+", _F),
    "markup_structure": re.compile(r"(?m)^\s*#{1,6}\s+\S|^\s*(?:---|===)\s*$"),
}


def scan_text(text: str) -> Counter[str]:
    found: Counter[str] = Counter()
    for kind, pattern in PATTERNS.items():
        n = len(pattern.findall(text))
        if n:
            found[kind] = n
    return found


def scan_many(texts: Iterable[str]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for text in texts:
        total.update(scan_text(text))
    return {kind: total[kind] for kind in PATTERNS if total[kind]}
