import json
from pathlib import Path

import pytest

from connbench.puzzles import Puzzle, normalize_record

FIXTURES = Path(__file__).parent / "fixtures"

RAW_2026_09_15 = {
    "id": 1187,
    "date": "2026-09-15",
    "answers": [
        {"level": -1, "group": "INSTRUCT", "members": ["COACH", "DIRECT", "GUIDE", "TRAIN"]},
        {"level": -1, "group": "PARTS OF A WHISTLING KETTLE", "members": ["HANDLE", "LID", "SPOUT", "WHISTLE"]},
        {"level": -1, "group": "UNDERCOVER OPERATIVE", "members": ["ASSET", "MOLE", "PLANT", "SLEEPER"]},
        {"level": -1, "group": "THINGS THAT APPEAR AT THE END", "members": ["CABOOSE", "DECEMBER", "DESSERT", "EPILOGUE"]},
    ],
}

RAW_WITH_LEVELS = {
    "id": 1,
    "date": "2023-06-12",
    "answers": [
        {"level": 0, "group": "WET WEATHER", "members": ["HAIL", "RAIN", "SLEET", "SNOW"]},
        {"level": 1, "group": "NBA TEAMS", "members": ["BUCKS", "HEAT", "JAZZ", "NETS"]},
        {"level": 2, "group": "KEYBOARD KEYS", "members": ["OPTION", "RETURN", "SHIFT", "TAB"]},
        {"level": 3, "group": "PALINDROMES", "members": ["KAYAK", "LEVEL", "MOM", "RACECAR"]},
    ],
}


@pytest.fixture
def raw():
    return json.loads(json.dumps(RAW_2026_09_15))


@pytest.fixture
def raw_levels():
    return json.loads(json.dumps(RAW_WITH_LEVELS))


@pytest.fixture
def puzzle() -> Puzzle:
    return normalize_record(RAW_2026_09_15)


@pytest.fixture
def puzzle_levels() -> Puzzle:
    return normalize_record(RAW_WITH_LEVELS)


@pytest.fixture
def archive(tmp_path, puzzle, puzzle_levels):
    from connbench.puzzles import write_puzzle

    root = tmp_path / "puzzles"
    write_puzzle(root, puzzle)
    write_puzzle(root, puzzle_levels)
    return root
