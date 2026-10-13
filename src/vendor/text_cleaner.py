# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
text_cleaner.py
===============
LLM-prompt cleaning for clinical discharge notes and assembled progress-note
sequences (vendored into the self-contained repo).

Applies a subset of note-cleaning suited to LLM input rather than subword
tokenizer input:

  Phase 1  (always): CP1252/encoding normalisation, stray quotes
  Phase 2  (always): bracketed de-identification marker removal, separator-only
                     lines, note-status markers
  Whitespace:        Per-line collapse; newlines preserved (structure matters
                     for LLM prompts); multiple blank lines collapsed to one.
"""

import re

# ── Phase 1: encoding ─────────────────────────────────────────────────────────

_CP1252_MAP = {
    "\x91": "'",   # left single quote
    "\x92": "'",   # right single quote / apostrophe
    "\x93": '"',   # left double quote
    "\x94": '"',   # right double quote
    "\x96": "-",   # en-dash
    "\x97": "-",   # em-dash
    "\xa0": " ",   # non-breaking space
    "\u200b": "",  # zero-width space
}

# Stray multiple-quote artefacts: """word → word  (from Phase 4)
_STRAY_QUOTES = re.compile(r'"{2,}')


def _phase1_encoding(text: str) -> str:
    for old, new in _CP1252_MAP.items():
        text = text.replace(old, new)
    # Strip remaining non-ASCII
    text = text.encode("ascii", errors="ignore").decode("ascii")
    # Stray multi-quote artefacts
    text = _STRAY_QUOTES.sub(" ", text)
    return text


# ── Phase 2: structural noise ─────────────────────────────────────────────────

# Bracketed de-identification markers: [**...**]
# Replaced with a single space (not a token) so sentence flow is preserved.
_DEID = re.compile(r"\[\*\*.*?\*\*\]")

# Note-status markers: **FINAL, **Addendum, *DRAFT
_NOTE_MARKERS = re.compile(r"(\s|^)\*{1,2}([A-Za-z]+)")

# Separator-only lines: lines consisting entirely of -, =, *, _, #, ~ (4+ chars)
# Handles: ---------, ==========, **********, ~~~~~~~~~~, ##########
# Uses MULTILINE so ^ and $ match each line.
_SEPARATOR_LINE = re.compile(r"^[ \t]*[-=*_#~]{4,}[ \t]*$", re.MULTILINE)

# Windows line endings
_CRLF = re.compile(r"\r\n?")

# Multiple consecutive blank lines → one blank line
_MULTI_BLANK = re.compile(r"\n{3,}")

# Per-line internal whitespace: multiple spaces/tabs on a single line → one space
# (Does NOT collapse newlines -- those are handled separately.)
_INLINE_SPACE = re.compile(r"[ \t]+")


def _phase2_structural(text: str) -> str:
    # Normalise line endings first
    text = _CRLF.sub("\n", text)
    # Remove separator-only lines (before de-id so [**---**] type patterns don't match)
    text = _SEPARATOR_LINE.sub("", text)
    # Remove bracketed de-identification markers
    text = _DEID.sub(" ", text)
    # Remove note-status markers
    text = _NOTE_MARKERS.sub(lambda m: f" {m.group(2)}", text)
    return text


def _normalise_whitespace(text: str) -> str:
    """Collapse per-line spaces; preserve newlines; collapse 3+ blank lines to 1."""
    lines = text.split("\n")
    lines = [_INLINE_SPACE.sub(" ", line).strip() for line in lines]
    text = "\n".join(lines)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


# ── Public API ────────────────────────────────────────────────────────────────

def clean_for_llm(text: str) -> str:
    """
    Clean a clinical note string for use as an LLM prompt.

    Preserves:
      - Newlines and paragraph structure (meaningful for the LLM)
      - Hyphenated lab/drug names (HCO3-24, beta-blocker)
      - List markers and clinical abbreviations

    Removes:
      - CP1252 / non-ASCII encoding artefacts
      - Bracketed [**de-identification**] markers
      - Separator-only lines (--------, ========, ********, etc.)
      - Note-status markers (**FINAL, *DRAFT)
      - Stray multi-quote artefacts
      - Redundant whitespace within lines
      - Excess blank lines (> 1 consecutive)

    Args:
        text: Raw note text from the clinical source or assembled progress notes.

    Returns:
        Cleaned string, preserving paragraph/line structure.
        Returns "" if nothing meaningful remains.
    """
    if not text or not isinstance(text, str):
        return ""

    text = _phase1_encoding(text)
    text = _phase2_structural(text)
    text = _normalise_whitespace(text)

    return text


def clean_note_sequence(notes: list[str]) -> list[str]:
    """
    Clean a list of progress notes (e.g. one per day).
    Returns list of cleaned strings; empty strings are filtered out.
    """
    return [c for note in notes if (c := clean_for_llm(note))]
