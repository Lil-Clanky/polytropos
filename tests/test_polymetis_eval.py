"""The offline search numbers of evals/polymetis may not get worse. Raise a floor when a change improves it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "polymetis"))
import retrieval  # noqa: E402

from polymetis.config import Settings  # noqa: E402

# distractors: (top 3, right tips, wrong tips, quiet on tasks without a skill)
FLOORS = {0: (31, 28, 1, 9), 141: (32, 30, 3, 8)}


@pytest.mark.parametrize("extra", sorted(FLOORS))
def test_the_search_keeps_its_offline_numbers(extra):
    skills, tasks = retrieval.load()
    m = retrieval.measure(skills, tasks, extra, Settings().min_score)
    at3, right, wrong, quiet = FLOORS[extra]
    assert m["at3"] >= at3 and m["right_tips"] >= right and m["quiet_none"] >= quiet
    assert m["wrong_tips"] <= wrong


def test_the_fixture_is_complete():
    skills, tasks = retrieval.load()
    names = {s["name"] for s in skills}
    assert len(skills) == 185 and sum(s["bundled"] for s in skills) == 44
    assert all(set(t["skills"]) <= names for t in tasks) and len({t["id"] for t in tasks}) == len(tasks)
