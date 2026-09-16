import pytest

from connbench.game import (
    INVALID_RESPONSE_HARD_STOP,
    MAX_CONSECUTIVE_FAILED_TURNS,
    GameOver,
    GameState,
    InvalidGuess,
    Outcome,
    Status,
    make_seed,
    seeded_shuffle,
)

SEED = make_seed("2026-09-15", "test__model", 1)


@pytest.fixture
def game(puzzle):
    return GameState.new(puzzle, SEED)


def test_shuffle_is_a_permutation(puzzle):
    board = seeded_shuffle(puzzle.words, SEED)
    assert sorted(board) == sorted(puzzle.words)
    assert board != puzzle.words


def test_shuffle_is_deterministic(puzzle):
    assert seeded_shuffle(puzzle.words, SEED) == seeded_shuffle(puzzle.words, SEED)
    assert seeded_shuffle(puzzle.words, SEED) != seeded_shuffle(puzzle.words, SEED + "x")


def test_shuffle_output_is_pinned(puzzle):
    """Freezes the exact shuffle. If this fails, historical runs no longer replay
    identically. That is a real breakage — do NOT update the expected value."""
    assert seeded_shuffle(puzzle.words, "2026-09-15|test__model|1") == (
        "WHISTLE", "DIRECT", "DECEMBER", "SPOUT", "PLANT", "ASSET", "SLEEPER", "TRAIN",
        "MOLE", "LID", "HANDLE", "GUIDE", "CABOOSE", "COACH", "DESSERT", "EPILOGUE",
    )


def test_seed_format():
    assert SEED == "2026-09-15|test__model|1"


def test_new_game(game):
    assert game.status is Status.IN_PROGRESS
    assert len(game.remaining_words) == 16
    assert game.mistakes_remaining == 4
    assert game.turn_number == 1


def test_correct_guess(game, puzzle):
    g = puzzle.groups[0]
    r = game.guess(["coach", " direct", "GUIDE", "Train"])
    assert r.outcome is Outcome.CORRECT
    assert r.group == g
    assert game.mistakes == 0
    assert set(game.remaining_words).isdisjoint(g.members)
    assert len(game.remaining_words) == 12
    assert game.history[0].group_name == g.name
    assert game.history[0].turn == 1


def test_one_away(game):
    r = game.guess(["COACH", "DIRECT", "GUIDE", "MOLE"])
    assert r.outcome is Outcome.ONE_AWAY
    assert game.mistakes == 1
    assert r.mistakes_remaining == 3


def test_incorrect(game):
    r = game.guess(["COACH", "DIRECT", "MOLE", "LID"])
    assert r.outcome is Outcome.INCORRECT
    assert game.mistakes == 1


def test_two_plus_two_is_incorrect_not_one_away(game):
    r = game.guess(["COACH", "DIRECT", "MOLE", "ASSET"])
    assert r.outcome is Outcome.INCORRECT


def test_win(game, puzzle):
    for g in puzzle.groups:
        r = game.guess(g.members)
    assert r.status is Status.WIN
    assert game.status is Status.WIN
    assert game.remaining_words == ()
    assert game.guesses_made == 4


def test_loss_on_fourth_mistake(game):
    wrong = [
        ["COACH", "DIRECT", "GUIDE", "MOLE"],
        ["COACH", "DIRECT", "GUIDE", "LID"],
        ["COACH", "DIRECT", "GUIDE", "ASSET"],
        ["COACH", "DIRECT", "GUIDE", "CABOOSE"],
    ]
    for w in wrong[:3]:
        assert game.guess(w).status is Status.IN_PROGRESS
    r = game.guess(wrong[3])
    assert r.status is Status.LOSS
    assert game.mistakes == 4


def test_moves_after_game_over_raise(game, puzzle):
    for g in puzzle.groups:
        game.guess(g.members)
    with pytest.raises(GameOver):
        game.guess(puzzle.groups[0].members)
    with pytest.raises(GameOver):
        game.charge_mistake()


@pytest.mark.parametrize(
    "words,match",
    [
        (["COACH", "DIRECT", "GUIDE"], "exactly 4"),
        (["COACH", "DIRECT", "GUIDE", "TRAIN", "LID"], "exactly 4"),
        (["COACH", "COACH", "GUIDE", "TRAIN"], "distinct"),
        (["COACH", "DIRECT", "GUIDE", "LOCOMOTIVE"], "not on the remaining board"),
    ],
)
def test_invalid_guesses_raise_and_count_but_do_not_penalize(game, words, match):
    with pytest.raises(InvalidGuess, match=match):
        game.guess(words)
    assert game.mistakes == 0
    assert game.invalid_responses == 1
    assert game.history == []
    assert game.guesses_made == 0


def test_solved_words_leave_the_board(game, puzzle):
    game.guess(puzzle.groups[0].members)
    with pytest.raises(InvalidGuess, match="not on the remaining board"):
        game.guess(["COACH", "LID", "MOLE", "CABOOSE"])


def test_repeat_guess_rejected_without_penalty(game):
    game.guess(["COACH", "DIRECT", "GUIDE", "MOLE"])
    assert game.mistakes == 1
    with pytest.raises(InvalidGuess, match="already been guessed"):
        game.guess(["MOLE", "GUIDE", "DIRECT", "COACH"])  # same set, different order
    assert game.mistakes == 1
    assert game.invalid_responses == 1


def test_charge_mistake_records_failed_turn(game):
    r = game.charge_mistake()
    assert r.outcome is Outcome.FAILED_TURN
    assert game.mistakes == 1
    assert game.consecutive_failed_turns == 1
    assert game.history[0].outcome is Outcome.FAILED_TURN
    assert game.history[0].words == ()
    assert game.guesses_made == 0
    assert game.turn_number == 2


def test_forfeit_after_three_consecutive_failed_turns(game):
    for _ in range(MAX_CONSECUTIVE_FAILED_TURNS - 1):
        assert game.charge_mistake().status is Status.IN_PROGRESS
    r = game.charge_mistake()
    assert r.status is Status.FORFEIT
    assert game.mistakes == 3  # forfeited before the 4th mistake


def test_valid_guess_resets_failed_turn_counter(game):
    game.charge_mistake()
    game.charge_mistake()
    game.guess(["COACH", "DIRECT", "GUIDE", "TRAIN"])
    assert game.consecutive_failed_turns == 0
    game.charge_mistake()
    assert game.status is Status.IN_PROGRESS
    assert game.mistakes == 3


def test_fourth_failed_turn_mistake_is_a_loss_not_forfeit(game):
    game.guess(["COACH", "DIRECT", "GUIDE", "MOLE"])  # mistake 1
    game.charge_mistake()  # 2
    game.guess(["COACH", "DIRECT", "GUIDE", "LID"])  # 3, resets counter
    r = game.charge_mistake()  # 4
    assert r.status is Status.LOSS


def test_invalid_responses_are_counted_separately_from_mistakes(game):
    for _ in range(5):
        game.record_invalid_response()
    assert game.invalid_responses == 5
    assert game.mistakes == 0
    assert game.status is Status.IN_PROGRESS


def test_hard_stop_trips_only_past_the_runner_maximum(game):
    for _ in range(INVALID_RESPONSE_HARD_STOP):
        game.record_invalid_response()
    assert game.status is Status.IN_PROGRESS  # exactly 9 is reachable by a correct runner
    game.record_invalid_response()
    assert game.status is Status.FORFEIT


def test_to_dict(game, puzzle):
    game.guess(puzzle.groups[1].members)
    d = game.to_dict()
    assert d["status"] == "in_progress"
    assert d["solved"] == [puzzle.groups[1].name]
    assert d["history"][0]["outcome"] == "correct"
    assert len(d["board"]) == 16
