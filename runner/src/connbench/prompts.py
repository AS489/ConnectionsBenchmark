"""Prompt variants and the output contract.

WHY VARIANTS ARE VERSIONED
--------------------------
This benchmark is designed to experiment on the prompt itself: change the wording,
collect another fortnight of games, compare. That only works if every run record
says which prompt produced it. A result with no prompt label cannot be compared
with anything, and the damage is retroactive — edit the wording once without
recording it and every game before and after becomes indistinguishable.

So: never edit an existing variant's text. Add a new one. `content_hash` is
recorded in every run and will disagree with the registry if someone edits a
variant in place, which is how that mistake gets caught.

This mirrors the model-snapshot pinning in models.yaml. A prompt is an input to
the experiment exactly as the model is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .game import GameState, GuessResult, Outcome

BOARD_PREFIX = "Words:"

# --------------------------------------------------------------------------- variants

V1_SYSTEM = """You are playing the New York Times game "Connections".

Rules:
- You are shown 16 words. They form exactly 4 hidden groups of 4 words each. Every word belongs to exactly one group.
- Each turn, you guess one group of 4 words.
- If your guess is correct, the group and its category name are revealed and removed from the board.
- If exactly 3 of your 4 words belong to the same group, you are told you are "one away". This costs a mistake.
- Otherwise the guess is incorrect. This costs a mistake.
- You may make at most 4 mistakes. On the 4th mistake the game is lost.
- Repeating an exact previous guess is not allowed. Guessing words that are no longer on the board is not allowed.
- Beware of red herrings: words that plausibly fit several groups. The most obvious grouping is often a trap.

Output contract — respond with ONLY a JSON object, no other text:
{"reasoning": "<brief reasoning>", "guess": ["WORD1", "WORD2", "WORD3", "WORD4"]}

The "guess" array must contain exactly 4 words copied exactly from the current board."""


@dataclass(frozen=True)
class PromptVariant:
    """One named, frozen prompt. Never edit `system` — add a new variant instead."""

    name: str
    system: str
    notes: str = ""

    @property
    def content_hash(self) -> str:
        """First 12 hex chars of the SHA-256 of the system prompt."""
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {"variant": self.name, "prompt_hash": self.content_hash}


VARIANTS: dict[str, PromptVariant] = {
    "v1": PromptVariant(
        name="v1",
        system=V1_SYSTEM,
        notes=(
            "The original prompt. Everything collected from 2026-09-26 onward used "
            "this, and it is the baseline every later variant is measured against."
        ),
    ),
}

DEFAULT_VARIANT = "v1"


def get_variant(name: str | None = None) -> PromptVariant:
    name = name or DEFAULT_VARIANT
    try:
        return VARIANTS[name]
    except KeyError:
        raise KeyError(
            f"unknown prompt variant {name!r}; known: {', '.join(sorted(VARIANTS))}"
        ) from None


# Kept so existing callers and tests that import SYSTEM_PROMPT keep working.
SYSTEM_PROMPT = V1_SYSTEM

RESPONSE_JSON_SCHEMA = {
    "name": "connections_guess",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "guess": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 4,
            },
        },
        "required": ["reasoning", "guess"],
        "additionalProperties": False,
    },
}

# A reasoning model that exhausts its token budget mid-thought returns prose and
# finish_reason="length". Telling it "X is not a word on the board" is actively
# misleading — it never got as far as guessing. Name the real problem instead.
TRUNCATED_REASON = (
    "your response was cut off before you produced a JSON object — you reached the "
    "token limit while still reasoning. Think in far fewer words and emit the JSON "
    "object first"
)


# --------------------------------------------------------------------------- rendering


def render_board(words) -> str:
    return f"{BOARD_PREFIX} " + ", ".join(words)


def opening_message(game: GameState) -> str:
    return (
        f"Here is the board. Find the 4 groups of 4.\n\n"
        f"{render_board(game.remaining_words)}\n\n"
        f"Mistakes remaining: {game.mistakes_remaining}. Give your first guess."
    )


def feedback_message(game: GameState, result: GuessResult) -> str:
    if result.outcome is Outcome.CORRECT and result.group is not None:
        head = (
            f"Correct! The group was \"{result.group.name}\": "
            + ", ".join(result.group.members)
            + "."
        )
    elif result.outcome is Outcome.ONE_AWAY:
        head = "One away — exactly 3 of those 4 words belong together. That cost a mistake."
    elif result.outcome is Outcome.INCORRECT:
        head = "Incorrect. That cost a mistake."
    else:  # FAILED_TURN
        head = "That turn produced no valid guess and was charged as a mistake."

    if game.status.terminal:
        return head + f"\n\nGame over: {game.status.value}."

    return (
        f"{head}\n\n"
        f"{render_board(game.remaining_words)}\n\n"
        f"Mistakes remaining: {game.mistakes_remaining}. Give your next guess."
    )


def retry_message(reason: str) -> str:
    return (
        f"Your response could not be used: {reason}\n\n"
        "Respond with ONLY a JSON object of the form "
        '{"reasoning": "...", "guess": ["WORD1", "WORD2", "WORD3", "WORD4"]} '
        "using exactly 4 words from the current board."
    )
