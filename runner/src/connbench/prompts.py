"""Prompt templates and the output contract.

The board is always rendered on a single line prefixed with ``Words:`` so that both
humans and the MockProvider can read it back unambiguously.
"""

from __future__ import annotations

from .game import GameState, GuessResult, Outcome

SYSTEM_PROMPT = """You are playing the New York Times game "Connections".

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

BOARD_PREFIX = "Words:"


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


# A reasoning model that exhausts its token budget mid-thought returns prose and
# finish_reason="length". Telling it "X is not a word on the board" is actively
# misleading — it never got as far as guessing. Name the real problem instead.
TRUNCATED_REASON = (
    "your response was cut off before you produced a JSON object — you reached the "
    "token limit while still reasoning. Think in far fewer words and emit the JSON "
    "object first"
)


def retry_message(reason: str) -> str:
    return (
        f"Your response could not be used: {reason}\n\n"
        "Respond with ONLY a JSON object of the form "
        '{"reasoning": "...", "guess": ["WORD1", "WORD2", "WORD3", "WORD4"]} '
        "using exactly 4 words from the current board."
    )
