"""The Connections game as a pure state machine. No I/O, no network, no randomness.

This is the correctness core. Every rule that the benchmark depends on lives here
and is pinned by tests. The runner drives it; it never reasons about rules itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .puzzles import Group, Puzzle, canonical

MAX_MISTAKES = 4

# A *turn* is the whole retry cycle: the runner feeds parse/validation errors back
# to the model up to twice, and only if the turn still fails does it call
# ``charge_mistake()``. That, not an individual invalid response, advances this
# counter. Three failed turns in a row forfeits the game.
MAX_CONSECUTIVE_FAILED_TURNS = 3

# Defensive backstop against a runner that never escalates to charge_mistake().
# It trips only when the count *exceeds* this. A correctly written runner cannot
# get there: 3 failed turns x 3 responses = exactly 9, and the forfeit guard in
# charge_mistake() fires first.
INVALID_RESPONSE_HARD_STOP = 9


class Outcome(str, Enum):
    CORRECT = "correct"
    ONE_AWAY = "one_away"
    INCORRECT = "incorrect"
    FAILED_TURN = "failed_turn"  # turn charged as a mistake with no valid guess


class Status(str, Enum):
    IN_PROGRESS = "in_progress"
    WIN = "win"
    LOSS = "loss"
    FORFEIT = "forfeit"

    @property
    def terminal(self) -> bool:
        return self is not Status.IN_PROGRESS


class InvalidGuess(ValueError):
    """The guess never reached the board. Carries a reason suitable to feed back to the model."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GameOver(RuntimeError):
    """A move was attempted on a finished game."""


@dataclass(frozen=True)
class GuessRecord:
    turn: int
    words: tuple[str, ...]  # canonical, in the order the model gave them; empty for FAILED_TURN
    outcome: Outcome
    group_name: str | None  # revealed on CORRECT
    mistakes_after: int

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "words": list(self.words),
            "outcome": self.outcome.value,
            "group": self.group_name,
            "mistakes_after": self.mistakes_after,
        }


@dataclass(frozen=True)
class GuessResult:
    outcome: Outcome
    group: Group | None
    status: Status
    mistakes_remaining: int


def seeded_shuffle(items: Iterable[str], seed: str) -> tuple[str, ...]:
    """Deterministic Fisher-Yates driven by SHA-256, not ``random``.

    Run reproducibility is a property of the benchmark. CPython's PRNG internals are
    not a contract; a hash is. ``test_shuffle_output_is_pinned`` freezes the exact
    output of this function — if it ever fails, historical runs no longer replay
    identically, and that is a breakage, not a test to update.
    """
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        digest = hashlib.sha256(f"{seed}:{i}".encode("utf-8")).digest()
        j = int.from_bytes(digest[:8], "big") % (i + 1)
        out[i], out[j] = out[j], out[i]
    return tuple(out)


def make_seed(puzzle_date: str, model_slug: str, attempt: int) -> str:
    return f"{puzzle_date}|{model_slug}|{attempt}"


@dataclass
class GameState:
    puzzle: Puzzle
    board: tuple[str, ...]  # display order, fixed for the whole game
    seed: str
    solved: list[Group] = field(default_factory=list)
    mistakes: int = 0
    history: list[GuessRecord] = field(default_factory=list)
    invalid_responses: int = 0
    consecutive_failed_turns: int = 0
    status: Status = Status.IN_PROGRESS

    @classmethod
    def new(cls, puzzle: Puzzle, seed: str) -> "GameState":
        return cls(puzzle=puzzle, board=seeded_shuffle(puzzle.words, seed), seed=seed)

    # ------------------------------------------------------------------ views

    @property
    def solved_names(self) -> set[str]:
        return {g.name for g in self.solved}

    @property
    def remaining_words(self) -> tuple[str, ...]:
        solved_words = {m for g in self.solved for m in g.members}
        return tuple(w for w in self.board if w not in solved_words)

    @property
    def mistakes_remaining(self) -> int:
        return MAX_MISTAKES - self.mistakes

    @property
    def turn_number(self) -> int:
        """1-based index of the turn about to be played."""
        return len(self.history) + 1

    @property
    def guesses_made(self) -> int:
        """Guesses that reached the board. Excludes failed turns and invalid responses."""
        return sum(1 for h in self.history if h.outcome is not Outcome.FAILED_TURN)

    @property
    def prior_guesses(self) -> set[frozenset[str]]:
        return {frozenset(h.words) for h in self.history if h.words}

    # ------------------------------------------------------------------ moves

    def _require_in_progress(self) -> None:
        if self.status.terminal:
            raise GameOver(f"game is over: {self.status.value}")

    def validate(self, words: Iterable[str]) -> tuple[str, ...]:
        """Canonicalize a guess and check it is legal. Raises InvalidGuess; does not count it."""
        canon = tuple(canonical(w) for w in words)
        if len(canon) != 4:
            raise InvalidGuess(f"a guess must contain exactly 4 words, got {len(canon)}")
        if len(set(canon)) != 4:
            raise InvalidGuess("a guess must contain 4 distinct words")
        remaining = set(self.remaining_words)
        missing = [w for w in canon if w not in remaining]
        if missing:
            raise InvalidGuess(
                "these words are not on the remaining board: " + ", ".join(missing)
            )
        if frozenset(canon) in self.prior_guesses:
            raise InvalidGuess("that exact group has already been guessed")
        return canon

    def record_invalid_response(self) -> None:
        """Count a response that never became a legal guess (unparseable or invalid).

        Trips the hard stop if the runner somehow never escalates to a failed turn.
        """
        self._require_in_progress()
        self.invalid_responses += 1
        if self.invalid_responses > INVALID_RESPONSE_HARD_STOP:
            self.status = Status.FORFEIT

    def guess(self, words: Iterable[str]) -> GuessResult:
        """Play a guess. Invalid guesses raise InvalidGuess (counted, not penalized)."""
        self._require_in_progress()
        try:
            canon = self.validate(words)
        except InvalidGuess:
            self.record_invalid_response()
            raise

        guessed = frozenset(canon)
        best_overlap = 0
        matched: Group | None = None
        for g in self.puzzle.groups:
            if g.name in self.solved_names:
                continue
            overlap = len(guessed & set(g.members))
            if overlap > best_overlap:
                best_overlap, matched = overlap, g

        if best_overlap == 4 and matched is not None:
            outcome, group = Outcome.CORRECT, matched
            self.solved.append(matched)
        elif best_overlap == 3:
            outcome, group = Outcome.ONE_AWAY, None
            self.mistakes += 1
        else:
            outcome, group = Outcome.INCORRECT, None
            self.mistakes += 1

        # Any guess that reached the board ends a run of failed turns.
        self.consecutive_failed_turns = 0
        self.history.append(
            GuessRecord(
                turn=self.turn_number,
                words=canon,
                outcome=outcome,
                group_name=group.name if group else None,
                mistakes_after=self.mistakes,
            )
        )
        self._update_status()
        return GuessResult(outcome, group, self.status, self.mistakes_remaining)

    def charge_mistake(self) -> GuessResult:
        """The runner exhausted its retries for this turn: charge it as a mistake."""
        self._require_in_progress()
        self.mistakes += 1
        self.consecutive_failed_turns += 1
        self.history.append(
            GuessRecord(
                turn=self.turn_number,
                words=(),
                outcome=Outcome.FAILED_TURN,
                group_name=None,
                mistakes_after=self.mistakes,
            )
        )
        self._update_status()
        if (
            self.status is Status.IN_PROGRESS
            and self.consecutive_failed_turns >= MAX_CONSECUTIVE_FAILED_TURNS
        ):
            self.status = Status.FORFEIT
        return GuessResult(Outcome.FAILED_TURN, None, self.status, self.mistakes_remaining)

    def _update_status(self) -> None:
        if len(self.solved) == 4:
            self.status = Status.WIN
        elif self.mistakes >= MAX_MISTAKES:
            self.status = Status.LOSS

    # ------------------------------------------------------------------ export

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "board": list(self.board),
            "status": self.status.value,
            "mistakes": self.mistakes,
            "invalid_responses": self.invalid_responses,
            "solved": [g.name for g in self.solved],
            "history": [h.to_dict() for h in self.history],
        }
