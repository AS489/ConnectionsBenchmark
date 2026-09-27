"""Prompt variants are experiment inputs and must be pinned like model snapshots."""

import json

import pytest

from connbench.prompts import DEFAULT_VARIANT, VARIANTS, PromptVariant, get_variant
from connbench.provider import MockProvider, ModelConfig
from connbench.runner import play
from connbench.scoring import check_prompt_hashes

CFG = ModelConfig(slug="t__m", openrouter_id="t/m")


def test_default_variant_exists():
    assert DEFAULT_VARIANT in VARIANTS
    assert get_variant().name == DEFAULT_VARIANT
    assert get_variant(None) is get_variant(DEFAULT_VARIANT)


def test_unknown_variant_raises_and_lists_known():
    with pytest.raises(KeyError, match="v1"):
        get_variant("does-not-exist")


def test_hash_is_stable_and_content_derived():
    a = PromptVariant("x", "hello")
    b = PromptVariant("y", "hello")
    c = PromptVariant("x", "hello!")
    assert a.content_hash == b.content_hash  # same text, same hash
    assert a.content_hash != c.content_hash  # edited text, different hash
    assert len(a.content_hash) == 12


def test_v1_hash_is_pinned():
    """Freezes the baseline prompt. If this fails, v1's text was edited in place —
    which silently makes every v1 result since incomparable. Add a NEW variant
    instead of editing this one, and do not update this expected value."""
    assert get_variant("v1").content_hash == "e4866c374ffa"


def test_run_record_carries_the_variant(puzzle):
    rec = play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle))
    assert rec["variant"] == "v1"
    assert rec["prompt_hash"] == get_variant("v1").content_hash


def test_the_recorded_variant_is_the_prompt_actually_sent(puzzle):
    """The label must reflect the system prompt that was really used, not a default
    written alongside it."""
    sent: list[str] = []

    class Capture(MockProvider):
        def complete(self, messages, cfg):
            sent.append(messages[0]["content"])
            return super().complete(messages, cfg)

    rec = play(puzzle, CFG, Capture("perfect", puzzle=puzzle))
    assert sent[0] == get_variant(rec["variant"]).system


def test_unknown_variant_fails_the_run_loudly(puzzle):
    with pytest.raises(KeyError):
        play(puzzle, CFG, MockProvider("perfect", puzzle=puzzle), prompt_variant="nope")


def test_hash_guard_flags_a_variant_edited_in_place():
    runs = [
        {"variant": "v1", "prompt_hash": "aaaaaaaaaaaa"},
        {"variant": "v1", "prompt_hash": "bbbbbbbbbbbb"},
    ]
    warnings = check_prompt_hashes(runs)
    assert len(warnings) == 1
    assert "NOT comparable" in warnings[0]


def test_hash_guard_flags_drift_from_the_registry():
    runs = [{"variant": "v1", "prompt_hash": "0123456789ab"}]
    warnings = check_prompt_hashes(runs)
    assert len(warnings) == 1 and "edited after those runs" in warnings[0]


def test_hash_guard_silent_when_consistent():
    h = get_variant("v1").content_hash
    assert check_prompt_hashes([{"variant": "v1", "prompt_hash": h}]) == []


def test_every_archived_record_carries_a_variant():
    """No record may exist without a prompt label — an unlabelled result cannot be
    compared with anything, and the gap cannot be repaired after the fact."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "data" / "runs"
    missing = [
        p.name for p in root.glob("*/*.json")
        if "variant" not in json.loads(p.read_text())
    ]
    assert missing == []
