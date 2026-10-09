"""Sanitiser for untrusted text that is written into a document.

Anything that came from the video (transcript, on-screen text, filenames) or from a model reading
it can carry text meant to manipulate an AI reader or to forge document structure. Two layers:

* `clean`: drop control characters and invisible Unicode (zero-width, bidi overrides, tag
  characters used to smuggle hidden instructions, ...).
* `neutralize_line`: backslash-escape the Markdown/HTML syntax that would create headings, rules,
  lists, quotes, links, images, raw HTML or anchor attributes.
"""

from __future__ import annotations

import re
import unicodedata

_DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}
_KEEP = {"\n", "\t"}
_LINE_BREAKS = {" ", " ", "\x0b", "\x0c", "\x85"}
_BLOCK_START = re.compile(r"^(?:[#>\-*+|=_~`]|\d+[.)](?:\s|$))")
_LINK_CHARS = re.compile(r"([\[\]])")


def clean(text: str) -> str:
    """Remove control and format characters; line separators become a plain newline."""
    out: list[str] = []
    for ch in text.replace("\r\n", "\n"):
        if ch in _LINE_BREAKS or ch == "\r":
            out.append("\n")
        elif ch in _KEEP or unicodedata.category(ch) not in _DROP_CATEGORIES:
            out.append(ch)
    return "".join(out)


def neutralize_line(line: str) -> str:
    """Escape one line of untrusted text so it cannot form Markdown or HTML structure."""
    s = _LINK_CHARS.sub(r"\\\1", line.replace("<", "&lt;")).replace("{#", "\\{#")
    stripped = s.lstrip()
    if _BLOCK_START.match(stripped) and not stripped.startswith("**"):
        return "\\" + stripped
    return s
