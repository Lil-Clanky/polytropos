"""BM25 over the skills of an index: name, category and description, all local."""

import math
import re
from collections import Counter
from functools import lru_cache

from .catalog import Skill

K1, B = 1.2, 0.75
NAME_WEIGHT = 2  # a word in a skill's name counts as often as this
_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset("""
a an and are as at be but by can could do does for from had has have how i if in into is it its me my
of on or our please should so than that the their them then there these this those to us was we what
when where which while who why will with would you your
aan als bij dan dat de die dit een en er het hoe ik in is je jij kan kun maar me met mijn moet naar niet
nog of om onze ook op te tot u van voor wat wie wil zijn
""".split())


def _stem(word: str) -> str:
    # shortcut: a few English suffixes only, so that "merges", "merging" and "merge" meet; a real
    # stemmer is the upgrade when the offline evaluation shows misses on word forms
    for suffix, keep in (("ing", 4), ("ed", 4), ("s", 3)):
        if word.endswith(suffix) and len(word) - len(suffix) >= keep:
            word = word[:-len(suffix)]
            break
    return word[:-1] if len(word) >= 5 and word.endswith("e") else word


def words(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall((text or "").lower()) if w not in STOPWORDS]


def _document(skill: Skill) -> list[str]:
    return words(skill.name) * NAME_WEIGHT + words(skill.category.replace("/", " ")) + words(skill.description)


class Index:
    def __init__(self, skills: tuple[Skill, ...]):
        self.skills = skills
        self.docs = [Counter(_document(s)) for s in skills]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if skills else 1.0
        df = Counter(w for d in self.docs for w in d)
        n = len(skills)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = set(words(query))
        out = []
        for doc, length in zip(self.docs, self.lengths):
            norm = K1 * (1 - B + B * length / (self.avg or 1.0))
            out.append(sum(self.idf[t] * doc[t] * (K1 + 1) / (doc[t] + norm) for t in terms if t in doc))
        return out


@lru_cache(maxsize=32)  # a session's index stays the same for many turns
def index_for(skills: tuple[Skill, ...]) -> Index:
    return Index(skills)


def rank(query: str, skills, *, pin=(), block=frozenset(), limit: int = 10) -> list[tuple[str, float]]:
    """The best skills for the query as (name, score), pinned skills that are in the index first."""
    skills = tuple(s for s in skills if s.name not in block)
    if not skills:
        return []
    scores = dict(zip((s.name for s in skills), index_for(skills).scores(query)))
    pinned = [(n, round(scores[n], 2)) for n in dict.fromkeys(pin) if n in scores]
    taken = {n for n, _ in pinned}
    rest = sorted(((n, round(sc, 2)) for n, sc in scores.items() if n not in taken and sc > 0),
                  key=lambda x: (-x[1], x[0]))
    return (pinned + rest)[:max(limit, len(pinned))]
