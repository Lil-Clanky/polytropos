# Polymetis v0.1.0 Implementation Plan

**Goal:** Add Polymetis, a standalone Hermes plugin that decides per session which skills and tools a model gets, and give `polytropos-router` the skip reasons, trace fields and request hook it needs to work with it.

**Spec:** `docs/superpowers/specs/2026-11-24-polymetis-design.md`

## Constraints

- Polymetis imports nothing from `plugins/polytropos`, `plugins/polytropos_router` or `shared/`. It runs without Polytropos and is not part of the pack. It is installed like `whatsapp-human-pace`.
- Standard library only. No network calls, no message text on disk, no message text in extra requests.
- Everything that changes a request is off by default. With `apply: []` Polymetis only decides and logs.
- Tests run without Hermes, without network, on Linux, macOS and Windows, Python 3.11 and 3.13.
- User-facing text: plain sentences, no em dashes, no slogans, no invented data.

## Tasks

1. **Settings** (`plugins/polymetis/config.py`, `tests/test_polymetis.py`): parse `apply`, `log`, `min_score`, `max_tips`, `pin`, `block`, `platform_levels`, `local_tools`, `ablation.share`, `ablation.until`; never echo a value in an error.
2. **Catalog and search** (`catalog.py`, `search.py`, `tests/test_polymetis.py`): parse `<available_skills>` (category rows, single rows, `[names only]` rows, qualified names); BM25 with name, category and description; pins first, blocks out.
3. **Decisions and sessions** (`decide.py`, `sessions.py`, tests): session level with platform levels, ablation arm and the one-time upgrade; per-turn candidates and tip; correction and repeat signals; bounded session store; fingerprint map.
4. **Log and hooks** (`log.py`, `hooks.py`, `__init__.py`, `tests/test_polymetis_hooks.py`): rotation at 5 MB, mode 0600; `pre_llm_call`, `pre_api_request`, `post_api_request`, `post_tool_call`, `on_session_end` build one record per turn; shadow mode changes nothing.
5. **Router** (`plugins/polytropos_router/client.py`, `trace.py`, tests): skip reasons, trace fields, rotation, the `local_request` hook point with fail-open errors.
6. **Report** (`report.py`, `hermes polymetis report`, tests): counts, unused skills, tip use, variant misses, ablation proposal and comparison.
7. **Tips** (`apply: [tips]`, tests).
8. **Local variants** (`shape.py`, `apply: [local_tools]`, tests): `full`, `lean`, `none`, `bare`, window step-down, hint removal, deterministic output.
9. **Ablation middleware** (tests): strip skills for sessions in the ablation arm.
10. **Offline evaluation** (`evals/polymetis/`, `tests/test_polymetis_eval.py`): tasks, Hermes skill fixture, recall table, guard test.
11. **Docs**: plugin README, Polytropos README section, `docs/reference.md`, the spike note.
12. **Verify**: sync check and tests on 3.11 and 3.13, CI on three systems, an independent review of the diff.
