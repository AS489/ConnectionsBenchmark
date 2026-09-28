import json

import pytest

from connbench.provider import MockProvider, ModelConfig, ProviderError, ProviderResponse
from connbench.runner import MAX_RESPONSES_PER_TURN, play, run_path, write_record

CFG = ModelConfig(slug="test__model", openrouter_id="test/model", max_cost_usd_per_run=0.5)


def test_perfect_mock_wins(puzzle):
    rec = play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle))
    assert rec["status"] == "win" and rec["solved"] is True
    assert rec["groups_solved"] == 4 and rec["mistakes_used"] == 0
    assert rec["guesses_made"] == 4 and rec["invalid_responses"] == 0
    assert rec["first_guess_correct"] is True
    assert rec["error"] is None
    assert rec["resolved_model"] == "mock/perfect"
    assert sorted(rec["groups_solved_by_name"].values()) == [1, 2, 3, 4]
    assert rec["seed"] == "2026-09-15|test__model|1"
    assert sorted(rec["board"]) == sorted(puzzle.words)


def test_naive_mock_loses(puzzle):
    rec = play(puzzle, CFG, MockProvider("naive"))
    assert rec["status"] == "loss" and rec["solved"] is False
    assert rec["mistakes_used"] == 4
    assert rec["invalid_responses"] == 0


def test_messy_mock_exercises_retry_paths(puzzle):
    rec = play(puzzle, CFG, MockProvider("messy", puzzle=puzzle))
    assert rec["status"] == "win"
    assert rec["mistakes_used"] == 1  # the one-away
    assert rec["invalid_responses"] == 2  # unparseable + off-board, both retried in-turn
    assert rec["guesses_made"] == 5
    outcomes = [t["outcome"] for t in rec["turns"]]
    assert outcomes == ["correct", "one_away", "correct", "correct", "correct"]
    retried = [t for t in rec["turns"] if len(t["responses"]) > 1][0]
    assert [r["error"] for r in retried["responses"]] == [
        'no JSON object with a 4-word "guess" array was found',
        "'ZZYZX' is not a word on the board",
        None,
    ]
    assert rec["turns"][0]["responses"][0]["reasoning"] == "fenced"


def test_broken_provider_is_an_error_not_a_loss(puzzle):
    rec = play(puzzle, CFG, MockProvider("broken"))
    assert rec["status"] == "error"
    assert rec["error"] == "mock provider is configured to fail"
    assert rec["solved"] is False


def test_three_failed_turns_forfeit(puzzle):
    garbage = ["nope"] * (MAX_RESPONSES_PER_TURN * 3)
    rec = play(puzzle, CFG, MockProvider("scripted", script=garbage))
    assert rec["status"] == "forfeit"
    assert rec["mistakes_used"] == 3
    assert rec["invalid_responses"] == 9
    assert [t["outcome"] for t in rec["turns"]] == ["failed_turn"] * 3
    assert rec["error"] is None  # the model failed, not the infrastructure


def test_script_exhaustion_mid_game_is_an_error(puzzle):
    g = puzzle.groups[0]
    rec = play(puzzle, CFG, MockProvider("scripted", script=[json.dumps({"guess": list(g.members)})]))
    assert rec["status"] == "error"
    assert rec["groups_solved"] == 1  # partial progress is preserved in the record
    assert "exhausted" in rec["error"]


def test_budget_guard(puzzle):
    class Pricey:
        name = "pricey"

        def complete(self, messages, cfg):
            return ProviderResponse(text="{}", cost_usd=0.30)

    rec = play(puzzle, CFG, Pricey())
    assert rec["status"] == "error"
    assert "exceeded cap" in rec["error"]


def test_api_key_is_redacted_from_records(puzzle):
    leak = "here is my key sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    rec = play(puzzle, CFG, MockProvider("scripted", script=[leak]))
    dumped = json.dumps(rec)
    assert "abcdefghijklmnop" not in dumped
    assert "sk-or-v1-***" in dumped


def test_backfill_flag_and_attempt(puzzle):
    rec = play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle), attempt=2, backfill=True)
    assert rec["backfill"] is True and rec["attempt"] == 2
    assert rec["seed"].endswith("|2")


def test_run_is_reproducible(puzzle):
    a = play(puzzle, CFG, MockProvider("messy", puzzle=puzzle))
    b = play(puzzle, CFG, MockProvider("messy", puzzle=puzzle))
    for k in ("started_at", "finished_at"):
        a.pop(k), b.pop(k)
    assert a == b


def test_write_record_idempotent(tmp_path, puzzle):
    rec = play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle))
    root = tmp_path / "runs"
    p = write_record(root, rec)
    assert p == run_path(root, "2026-09-15", "test__model") == root / "2026-09-15" / "test__model.json"
    assert write_record(root, rec) is None
    assert write_record(root, rec, force=True) == p
    assert run_path(root, "2026-09-15", "m", attempt=2).name == "m__a2.json"


def test_truncated_response_gets_truncation_feedback_not_a_word_error(puzzle):
    """A reasoning model that runs out of tokens mid-thought must be told *that*,
    not that some word it never guessed isn't on the board. Regression from a real
    nvidia/nemotron-3-super-120b run on 2026-09-26 that forfeited with 9 invalids."""
    from connbench.prompts import TRUNCATED_REASON
    from connbench.provider import ProviderResponse

    class Rambler:
        name = "rambler"

        def complete(self, messages, cfg):
            return ProviderResponse(
                text="We need to find groups of 4. Let's see: STARCH, maybe GUM, or",
                finish_reason="length",
            )

    rec = play(puzzle, CFG, Rambler())
    assert rec["status"] == "forfeit"
    first = rec["turns"][0]["responses"][0]
    assert first["truncated"] is True
    assert first["error"] == TRUNCATED_REASON
    assert "not a word on the board" not in first["error"]


def test_empty_response_never_becomes_an_empty_assistant_message(puzzle):
    """Some providers (Cohere) reject an empty assistant message with HTTP 400,
    which poisons every later turn. Regression from cohere/north-mini-code on
    2026-09-27: it spent its whole budget on reasoning, returned content="", and
    the retry died with a 400."""
    from connbench.provider import ProviderResponse

    seen: list[list[dict]] = []

    class EmptyContent:
        name = "empty"

        def complete(self, messages, cfg):
            seen.append([dict(m) for m in messages])
            return ProviderResponse(
                text="", reasoning_text="I should think about this...",
                finish_reason="length",
            )

    rec = play(puzzle, CFG, EmptyContent())
    assert rec["status"] == "forfeit"
    # No conversation we ever sent may contain an empty assistant message.
    for convo in seen:
        for m in convo:
            if m["role"] == "assistant":
                assert m["content"].strip(), "sent an empty assistant message"
    # The chain of thought is captured even though content was empty.
    assert rec["turns"][0]["responses"][0]["reasoning_text"] == "I should think about this..."


def test_budget_starved_run_is_marked_as_such(puzzle):
    """A run where EVERY response hit the token ceiling measured our budget, not the
    model. Regression from 2026-09-28 puzzle #1200, where two models forfeited with
    nine truncated, empty responses while every model with headroom solved it."""
    from connbench.provider import ProviderResponse

    class Truncating:
        name = "trunc"

        def complete(self, messages, cfg):
            return ProviderResponse(text="", finish_reason="length",
                                    reasoning_text="thinking and thinking...")

    rec = play(puzzle, CFG, Truncating())
    assert rec["status"] == "forfeit"
    assert rec["budget_starved"] is True
    assert rec["truncated_responses"] == rec["usage"]["requests"]


def test_healthy_run_is_not_marked_budget_starved(puzzle):
    rec = play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle))
    assert rec["budget_starved"] is False
    assert rec["truncated_responses"] == 0
