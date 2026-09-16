"""Turn a model's raw response into four canonical board words.

Strict ``response_format: json_schema`` handles the compliant models. This is the
tolerant fallback for everyone else: it strips code fences, digs JSON out of
surrounding prose, tolerates a bare list, and fuzzy-matches the returned words back
onto the board. It returns four board words or raises ParseError; the caller owns
the retry policy.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Iterable

from .puzzles import canonical

GUESS_KEYS = ("guess", "words", "answer", "group", "selection", "final_guess", "final_answer")
REASONING_KEYS = ("reasoning", "rationale", "explanation", "thinking")

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.DOTALL)
_ARTICLE_RE = re.compile(r"^(THE|A|AN)\s+", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_SPLIT_RE = re.compile(r"\s*(?:,|;|\||\bAND\b|\n)\s*", re.IGNORECASE)


class ParseError(ValueError):
    """The response did not yield four board words. ``reason`` is safe to feed back."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason




def _strip_fences(text: str) -> str:
    """Prefer the contents of the last code fence if any exist; otherwise the raw text."""
    fences = _FENCE_RE.findall(text)
    if fences:
        return fences[-1].strip()
    # Unterminated fence: drop the opening marker.
    return re.sub(r"```[a-zA-Z0-9_-]*\s*", "", text).strip()


def _json_candidates(text: str):
    """Yield every JSON value that can be decoded starting at a '{' or '[' in the text,
    latest first (models put the answer last)."""
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"[\[{]", text)]
    for start in reversed(starts):
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        yield value


def _find_key(d: dict, keys: Iterable[str]):
    lowered = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in lowered:
            return lowered[k]
    return None


def _as_word_list(value) -> list[str] | None:
    """Coerce a candidate guess value into a list of strings, or None if it can't be."""
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return value
        # list of {"word": ...} objects
        if all(isinstance(v, dict) for v in value):
            out = []
            for v in value:
                w = _find_key(v, ("word", "text", "name"))
                if not isinstance(w, str):
                    return None
                out.append(w)
            return out
        return None
    if isinstance(value, str):
        parts = [p for p in _SPLIT_RE.split(value) if p.strip()]
        return parts if len(parts) > 1 else None
    return None


def extract_guess_strings(text: str) -> list[str]:
    """Pull the raw guess strings out of a response, before board matching."""
    body = _strip_fences(text)

    for value in _json_candidates(body):
        if isinstance(value, dict):
            for key in GUESS_KEYS:
                v = _find_key(value, (key,))
                words = _as_word_list(v) if v is not None else None
                if words:
                    return words
            # single-key object whose value is a list, e.g. {"Group 1": [...]}
            if len(value) == 1:
                words = _as_word_list(next(iter(value.values())))
                if words:
                    return words
        elif isinstance(value, list):
            words = _as_word_list(value)
            if words:
                return words

    # Last resort: a labelled line like "Guess: A, B, C, D" or a bare comma list.
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for ln in reversed(lines):
        ln = re.sub(r"^[-*\d.)\s]*", "", ln)
        ln = re.sub(r"^(final\s+)?(guess|answer|group|words)\s*[:=-]\s*", "", ln, flags=re.I)
        parts = [p.strip(" \"'[]()") for p in _SPLIT_RE.split(ln) if p.strip(" \"'[]()")]
        if len(parts) == 4:
            return parts

    raise ParseError("no JSON object with a 4-word \"guess\" array was found")


def extract_reasoning(text: str) -> str | None:
    body = _strip_fences(text)
    for value in _json_candidates(body):
        if isinstance(value, dict):
            r = _find_key(value, REASONING_KEYS)
            if isinstance(r, str) and r.strip():
                return r.strip()
    return None


def _loose(word: str) -> str:
    return _NON_ALNUM_RE.sub("", canonical(word))


def match_word(raw: str, board: Iterable[str]) -> str:
    """Map one returned string onto a canonical board word, or raise ParseError."""
    board = [canonical(b) for b in board]
    by_canon = {b: b for b in board}
    by_loose: dict[str, list[str]] = {}
    for b in board:
        by_loose.setdefault(_loose(b), []).append(b)

    tiers = [canonical(raw)]
    stripped = canonical(raw).strip(" \"'`.,;:!?()[]{}")
    tiers.append(stripped)
    tiers.append(_ARTICLE_RE.sub("", stripped))
    for t in tiers:
        if t in by_canon:
            return t
    for t in tiers:
        hits = by_loose.get(_loose(t))
        if hits and len(hits) == 1:
            return hits[0]
    close = difflib.get_close_matches(stripped, board, n=2, cutoff=0.85)
    if len(close) == 1 or (len(close) > 1 and _similar(stripped, close[0]) > _similar(stripped, close[1])):
        return close[0]
    raise ParseError(f"{raw!r} is not a word on the board")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def parse_guess(text: str, board: Iterable[str]) -> tuple[str, str, str, str]:
    """Parse a response into exactly four distinct canonical board words."""
    board = tuple(canonical(b) for b in board)
    raw_words = extract_guess_strings(text)
    if len(raw_words) != 4:
        raise ParseError(f"a guess must contain exactly 4 words, got {len(raw_words)}")
    matched = tuple(match_word(w, board) for w in raw_words)
    if len(set(matched)) != 4:
        raise ParseError("the 4 words must be distinct board words")
    return matched  # type: ignore[return-value]
