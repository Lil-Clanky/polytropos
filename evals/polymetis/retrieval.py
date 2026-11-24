"""Offline check of Polymetis's skill search: no model, no network, no Hermes.

For each task in tasks.json it ranks the skills of an index the way Polymetis does, over Hermes's 44
bundled skills plus 0, 50 or all 141 optional skills as distractors, and prints how often the right
skill comes first, in the top 3 and in the top 10, and how many tips a min_score would give.

Usage: python evals/polymetis/retrieval.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "plugins"))

from polymetis import decide, search  # noqa: E402
from polymetis.catalog import Skill  # noqa: E402
from polymetis.config import Settings  # noqa: E402

DISTRACTORS = (0, 50, 141)
THRESHOLDS = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)


def load() -> tuple[list[dict], list[dict]]:
    skills = json.loads((HERE / "skills.json").read_text(encoding="utf-8"))
    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
    return skills, tasks


def pool(skills: list[dict], extra: int) -> tuple[Skill, ...]:
    """The bundled skills and ``extra`` optional ones, spread evenly over the optional list."""
    bundled = [s for s in skills if s["bundled"]]
    optional = [s for s in skills if not s["bundled"]]
    picked = [optional[i * len(optional) // extra] for i in range(extra)] if extra else []
    return tuple(Skill(s["name"], s["description"], s["category"]) for s in bundled + picked)


def measure(skills: list[dict], tasks: list[dict], extra: int, min_score: float) -> dict:
    index = pool(skills, extra)
    settings = Settings(min_score=min_score)
    out = {"tasks": 0, "at1": 0, "at3": 0, "at10": 0, "right_tips": 0, "wrong_tips": 0, "quiet_none": 0,
           "none_tasks": 0, "by_lang": {}}
    for task in tasks:
        ranked = search.rank(task["text"], index, limit=10)
        names = [n for n, _ in ranked]
        good = set(task["skills"])
        tip = decide.tip(settings, ranked)
        if not good:
            out["none_tasks"] += 1
            out["quiet_none"] += not tip
            out["wrong_tips"] += bool(tip)
            continue
        lang = out["by_lang"].setdefault(task["lang"], {"tasks": 0, "at1": 0, "at3": 0, "at10": 0})
        for bucket in (out, lang):
            bucket["tasks"] += 1
            bucket["at1"] += bool(good & set(names[:1]))
            bucket["at3"] += bool(good & set(names[:3]))
            bucket["at10"] += bool(good & set(names[:10]))
        out["right_tips"] += bool(good & set(tip))
        out["wrong_tips"] += bool(tip) and not good & set(tip)
    return out


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole}"


def main() -> None:
    skills, tasks = load()
    default = Settings().min_score
    print(f"{len(tasks)} tasks, {sum(1 for t in tasks if not t['skills'])} of them without a skill.\n")
    print("| Index | Language | Top 1 | Top 3 | Top 10 |")
    print("|---|---|---|---|---|")
    for extra in DISTRACTORS:
        m = measure(skills, tasks, extra, default)
        for lang, b in sorted(m["by_lang"].items()):
            print(f"| {44 + extra} skills | {lang} | {_pct(b['at1'], b['tasks'])} | {_pct(b['at3'], b['tasks'])} | "
                  f"{_pct(b['at10'], b['tasks'])} |")
    print("\nTips per min_score (right: a tipped skill fits the task; wrong: a tip on a task it does not fit,"
          " or on a task that needs none):\n")
    print("| min_score | Index | Right tips | Wrong tips | Quiet on tasks without a skill |")
    print("|---|---|---|---|---|")
    for threshold in THRESHOLDS:
        for extra in DISTRACTORS:
            m = measure(skills, tasks, extra, threshold)
            print(f"| {threshold:g} | {44 + extra} skills | {_pct(m['right_tips'], m['tasks'])} | "
                  f"{_pct(m['wrong_tips'], len(tasks))} | {_pct(m['quiet_none'], m['none_tasks'])} |")


if __name__ == "__main__":
    main()
