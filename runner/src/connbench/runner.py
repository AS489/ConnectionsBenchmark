"""Orchestration: play one puzzle with one model and emit a run record.

Turn structure (this is what the forfeit guard depends on):

    for each turn:
        for up to MAX_RESPONSES_PER_TURN responses:
            ask the model -> parse -> game.guess()
            on ParseError / InvalidGuess: feed the error back and try again
        if no response became a legal guess: game.charge_mistake()

Only ``charge_mistake`` advances the consecutive-failed-turn counter.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .game import GameState, InvalidGuess, Status, make_seed
from .parse import ParseError, extract_reasoning, parse_guess
from .prompts import SYSTEM_PROMPT, feedback_message, opening_message, retry_message
from .provider import ModelConfig, Provider, ProviderError, redact
from .puzzles import Puzzle
from .scoring import RUN_SCHEMA_VERSION, score_game

MAX_RESPONSES_PER_TURN = 3  # one attempt + two retries with the error fed back


class BudgetExceeded(ProviderError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    requests: int = 0

    def add(self, r) -> None:
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.reasoning_tokens += r.reasoning_tokens
        self.cost_usd += r.cost_usd
        self.latency_ms += r.latency_ms
        self.requests += 1

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class TurnLog:
    turn: int
    responses: list[dict] = field(default_factory=list)  # every model response this turn
    outcome: str | None = None
    words: list[str] = field(default_factory=list)
    group: str | None = None
    mistakes_after: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def run_path(runs_root: str | Path, date: str, slug: str, attempt: int = 1) -> Path:
    suffix = "" if attempt == 1 else f"__a{attempt}"
    return Path(runs_root) / date / f"{slug}{suffix}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def play(
    puzzle: Puzzle,
    cfg: ModelConfig,
    provider: Provider,
    *,
    attempt: int = 1,
    backfill: bool = False,
) -> dict:
    """Play one full game and return the run record (never raises for model or infra failures)."""
    seed = make_seed(puzzle.date, cfg.slug, attempt)
    game = GameState.new(puzzle, seed)
    usage = Usage()
    turns: list[TurnLog] = []
    resolved_model: str | None = None
    error: str | None = None
    started = _now()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": opening_message(game)},
    ]

    try:
        while not game.status.terminal:
            log = TurnLog(turn=game.turn_number)
            turns.append(log)
            result = None
            for _ in range(MAX_RESPONSES_PER_TURN):
                resp = provider.complete(messages, cfg)
                usage.add(resp)
                resolved_model = resp.model or resolved_model
                if usage.cost_usd > cfg.max_cost_usd_per_run:
                    raise BudgetExceeded(
                        f"run cost ${usage.cost_usd:.4f} exceeded cap ${cfg.max_cost_usd_per_run:.2f}"
                    )
                text = redact(resp.text)
                messages.append({"role": "assistant", "content": text})
                entry = {
                    "text": text,
                    "reasoning": extract_reasoning(text),
                    "latency_ms": resp.latency_ms,
                    "prompt_tokens": resp.prompt_tokens,
                    "completion_tokens": resp.completion_tokens,
                    "reasoning_tokens": resp.reasoning_tokens,
                    "cost_usd": resp.cost_usd,
                    "finish_reason": resp.finish_reason,
                    "error": None,
                }
                log.responses.append(entry)
                try:
                    words = parse_guess(text, game.remaining_words)
                except ParseError as e:
                    game.record_invalid_response()
                    entry["error"] = e.reason
                    if game.status.terminal:
                        break
                    messages.append({"role": "user", "content": retry_message(e.reason)})
                    continue
                try:
                    result = game.guess(words)
                except InvalidGuess as e:
                    entry["error"] = e.reason
                    if game.status.terminal:
                        break
                    messages.append({"role": "user", "content": retry_message(e.reason)})
                    continue
                break

            if game.status.terminal and result is None:
                log.outcome = "hard_stop"
                break
            if result is None:
                result = game.charge_mistake()
            rec = game.history[-1]
            log.outcome = rec.outcome.value
            log.words = list(rec.words)
            log.group = rec.group_name
            log.mistakes_after = rec.mistakes_after
            messages.append({"role": "user", "content": feedback_message(game, result)})
    except ProviderError as e:
        error = redact(str(e))
    except Exception as e:  # a bug in the runner is still an infrastructure failure, not a loss
        error = redact(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "date": puzzle.date,
        "puzzle_id": puzzle.id,
        "model": cfg.slug,
        "openrouter_id": cfg.openrouter_id,
        "resolved_model": resolved_model,
        "provider": getattr(provider, "name", type(provider).__name__),
        "attempt": attempt,
        "backfill": backfill,
        "seed": seed,
        "board": list(game.board),
        "started_at": started,
        "finished_at": _now(),
        "error": error,
        **score_game(game),
        "usage": usage.to_dict(),
        "turns": [t.to_dict() for t in turns],
    }
    if error is not None:
        record["status"] = "error"
        record["solved"] = False
    return record


def write_record(runs_root: str | Path, record: dict, *, force: bool = False) -> Path | None:
    path = run_path(runs_root, record["date"], record["model"], record["attempt"])
    if path.exists() and not force:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
