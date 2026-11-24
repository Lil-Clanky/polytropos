# Polymetis offline evaluation

This measures the skill search in `plugins/polymetis` without a model, a network or Hermes. It ranks the skills of an index for each task the way Polymetis does and counts how often the right skill comes out on top, and how many tips a `min_score` would give.

```
python evals/polymetis/retrieval.py
```

`tests/test_polymetis_eval.py` runs the same measurement and fails when the numbers get worse.

## Files

- `tasks.json`: 48 tasks, 32 in English and 16 in Dutch. 38 name the skills that fit them; 10 need no skill, and some of those share words with a skill on purpose ("merge sort", "a whole note").
- `skills.json`: the names, categories and descriptions of the 185 skills in Hermes Agent, as its index shows them: the 44 bundled skills and 141 optional ones. They come from Hermes Agent at commit 2506f5b0 (github.com/NousResearch/hermes-agent), MIT License, Copyright (c) 2025 Nous Research.
- `retrieval.py`: the measurement. The index holds the 44 bundled skills, plus 0, 50 or all 141 optional skills as distractors.

## Results

Measured on 2026-11-24 with the default `min_score` of 5.

| Index | Language | Top 1 | Top 3 | Top 10 |
|---|---|---|---|---|
| 44 skills | en | 21/26 | 23/26 | 24/26 |
| 44 skills | nl | 8/12 | 8/12 | 8/12 |
| 94 skills | en | 21/26 | 24/26 | 24/26 |
| 94 skills | nl | 7/12 | 8/12 | 8/12 |
| 185 skills | en | 20/26 | 23/26 | 24/26 |
| 185 skills | nl | 8/12 | 9/12 | 9/12 |

| min_score | Index | Right tips | Wrong tips | Quiet on tasks without a skill |
|---|---|---|---|---|
| 4 | 44 skills | 28/38 | 1/48 | 9/10 |
| 4 | 185 skills | 31/38 | 5/48 | 8/10 |
| 5 | 44 skills | 28/38 | 1/48 | 9/10 |
| 5 | 185 skills | 30/38 | 3/48 | 8/10 |
| 6 | 44 skills | 16/38 | 0/48 | 10/10 |
| 6 | 185 skills | 25/38 | 4/48 | 9/10 |

A right tip names at least one skill that fits the task. A wrong tip names only skills that do not fit, or comes on a task that needs no skill. At 5 the tips stay mostly right on small and large indexes; at 6 the small index loses almost half of its tips.

## What the numbers say

- English tasks find their skill in most cases. The misses use words that no description holds: "Where is my iPad?" for `findmy`, "drive from Utrecht to Groningen" for `maps`, "doesn't sound like an AI wrote it" for `humanizer`.
- Dutch tasks only work when they contain an English word or a product name, such as GitHub, PDF, YouTube, Obsidian, arXiv or inbox. "Zet een herinnering" and "vergadernotities" find nothing. A missed tip leaves the turn as Hermes runs it without Polymetis; for a local model it means the session starts at `none`.
- The wrong tips come from words that a skill shares with an unrelated question, such as "music" and "note" in a question about music theory.

## Limits

The tasks were written by the author of the search, with the skill descriptions at hand, so they flatter it. The scores depend on the size of the index, which is why the distractor rows exist. Nothing here says whether a tip helps a model; that needs the live evaluation in `docs/superpowers/specs/2026-11-24-polymetis-design.md`, which has not been run.
