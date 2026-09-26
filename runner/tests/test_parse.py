import json

import pytest

from connbench.parse import ParseError, extract_reasoning, match_word, parse_guess

from conftest import FIXTURES

CORPUS = json.loads((FIXTURES / "parse" / "cases.json").read_text())
BOARD = CORPUS["board"]


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["file"])
def test_corpus(case):
    text = (FIXTURES / "parse" / case["file"]).read_text()
    board = case.get("board", BOARD)
    if case.get("error"):
        with pytest.raises(ParseError):
            parse_guess(text, board)
    else:
        assert list(parse_guess(text, board)) == case["expected"]


def test_every_fixture_file_has_a_case():
    files = {p.name for p in (FIXTURES / "parse").glob("*.txt")}
    cases = {c["file"] for c in CORPUS["cases"]}
    assert files == cases


def test_parse_error_reason_is_feedback_safe():
    with pytest.raises(ParseError) as ei:
        parse_guess('{"guess": ["COACH", "DIRECT", "GUIDE", "LOCOMOTIVE"]}', BOARD)
    assert "LOCOMOTIVE" in ei.value.reason


def test_extract_reasoning():
    assert extract_reasoning('```json\n{"reasoning": "kettle parts", "guess": []}\n```') == "kettle parts"
    assert extract_reasoning('{"guess": ["A"]}') is None
    assert extract_reasoning("no json here") is None


def test_match_word_tiers():
    assert match_word("coach", BOARD) == "COACH"
    assert match_word("The Caboose", BOARD) == "CABOOSE"
    assert match_word("'LID'", BOARD) == "LID"
    assert match_word("Ice Cream", ["ICE-CREAM", "TEA"]) == "ICE-CREAM"
    with pytest.raises(ParseError):
        match_word("ZEBRA", BOARD)


def test_ambiguous_close_match_is_rejected():
    # "BAT" is equally close to BATS and BATH; refuse to guess.
    with pytest.raises(ParseError):
        match_word("BAT", ["BATS", "BATH", "CAT", "DOG"])
