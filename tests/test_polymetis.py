import copy
import datetime
import json
import os
import time

import pytest
import yaml
from polymetis_data import INDEX, ONESHOT, TOOLS

from polymetis import catalog, decide, log, report, search, shape
from polymetis.catalog import Skill
from polymetis.config import Settings, parse_settings

DAY = 86400.0


def settings(**values):
    return parse_settings(lambda key, default=None: values.get(key, default))


# --- settings ---

def test_settings_default_to_shadow_mode():
    s, problems = settings()
    assert problems == [] and s == Settings()
    assert s.apply == frozenset() and s.log is True and s.ablation_active() is False


def test_settings_read_every_key():
    s, problems = settings(**{"apply": ["tips", "local_tools"], "log": False, "min_score": 2.5, "max_tips": 1,
                              "pin": ["a", "a", "b"], "block": ["c"], "platform_levels": {"whatsapp": "none"},
                              "local_tools": ["terminal"], "ablation.share": 0.25, "ablation.until": "2026-12-01"})
    assert problems == []
    assert s.apply == {"tips", "local_tools"} and s.log is False and s.min_score == 2.5 and s.max_tips == 1
    assert s.pin == ("a", "b") and s.block == {"c"} and s.platform_levels == {"whatsapp": "none"}
    assert s.local_tools == ("terminal",) and s.ablation_share == 0.25
    assert s.ablation_until == datetime.date(2026, 12, 1)
    assert s.ablation_active(datetime.date(2026, 12, 1)) and not s.ablation_active(datetime.date(2026, 12, 2))


@pytest.mark.parametrize("value", ["2027-01-05", "2027-01-05 23:59:00", "'2027-01-05'"])
def test_settings_take_a_yaml_date(value):
    s, problems = settings(**{"ablation.until": yaml.safe_load(f"until: {value}")["until"]})
    assert s.ablation_until == datetime.date(2027, 1, 5) and problems == []


def test_a_bad_value_keeps_its_default_and_the_problem_never_quotes_the_value():
    s, problems = settings(min_score="sk-secret-value", apply=["everything"], log="yes",
                           platform_levels={"cli": "most"}, **{"ablation.until": "soon"})
    assert s == Settings()
    assert len(problems) == 5 and not any("sk-secret-value" in p or "soon" in p for p in problems)
    assert any(p.startswith("min_score:") for p in problems)


def test_a_failing_config_read_means_the_default():
    def get(key, default=None):
        raise RuntimeError("config.yaml is broken")
    assert parse_settings(get) == (Settings(), [])


# --- the skill index ---

def test_parse_index_reads_rows_categories_and_names_only_lines():
    skills = catalog.parse_index(INDEX)
    assert [s.name for s in skills] == ["github-pr-workflow", "github-issues", "ascii-art", "gif-search",
                                        "youtube-content"]
    assert skills[0] == Skill("github-pr-workflow", "Open, review and merge pull requests with gh", "github")
    assert skills[2].category == ""  # a one-skill category is a top-level row
    assert skills[3] == Skill("gif-search", "", "media")


def test_parse_index_without_an_index_is_none():
    assert catalog.parse_index("You are Hermes.") is None
    assert catalog.parse_index("") is None


@pytest.mark.parametrize("prompt", [INDEX, ONESHOT])
def test_strip_skills_leaves_no_index_and_no_pointer(prompt):
    out = catalog.strip_skills(prompt)
    for gone in ("<available_skills>", "## Skills", "skill_view", "skill_manage", "Only proceed", "(Categories"):
        assert gone not in out
    assert out.startswith("You are Hermes") and "## Memory" in out


def test_strip_skills_keeps_the_rest_of_the_prompt():
    out = catalog.strip_skills(INDEX)
    assert "The user likes short answers." in out and "The docs are the reference." in out
    assert "\n\n\n" not in out


def test_strip_skills_takes_linear_time_on_a_long_line():
    text = "word " * 8_000  # one line of 40k characters without a pointer
    started = time.perf_counter()
    assert catalog.strip_skills(text) == text
    assert time.perf_counter() - started < 0.5  # a pattern tried at every position took 5 seconds here


def test_keep_skills_lists_only_the_named_skills():
    out = catalog.keep_skills(INDEX, ["ascii-art", "not-installed"])
    assert [s.name for s in catalog.parse_index(out)] == ["ascii-art"]
    assert "github-issues" not in out and "## Memory" in out


def test_keep_skills_with_no_match_strips_the_index():
    assert catalog.keep_skills(INDEX, []) == catalog.strip_skills(INDEX)


def test_text_of_joins_text_parts_only():
    parts = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "x"}},
             {"text": "b"}, {"cachePoint": {"type": "default"}},  # these two as Bedrock sends them
             {"type": "input_text", "text": "c"}]  # as the Responses API takes it
    assert catalog.text_of(parts) == "a\nb\nc" and catalog.text_of(None) == ""


def test_strip_hints():
    assert catalog.strip_hints("Fix it\n\n<skill-hint>Relevant skill: x.</skill-hint>") == "Fix it"


# --- search ---

def test_word_forms_meet():
    assert {search._stem(w) for w in ("merge", "merges", "merging", "merged")} == {"merg"}
    assert search._stem("remind") == search._stem("reminders") == "remind"
    assert search._stem("notes") != search._stem("not") and search._stem("user") == "user"


def test_rank_puts_the_matching_skill_first():
    skills = catalog.parse_index(INDEX)
    ranked = search.rank("Can you merge my pull requests?", skills)
    assert ranked[0][0] == "github-pr-workflow" and ranked[0][1] > 0
    assert all(score > 0 for _, score in ranked)


def test_rank_reads_dutch_questions_too():
    assert search.rank("Maak een pull request voor deze branch", catalog.parse_index(INDEX))[0][0] == "github-pr-workflow"


def test_rank_pins_first_and_leaves_blocked_out():
    skills = catalog.parse_index(INDEX)
    ranked = search.rank("merge my pull request", skills, pin=["ascii-art", "missing"], block={"github-pr-workflow"})
    assert ranked[0][0] == "ascii-art"
    assert "github-pr-workflow" not in dict(ranked) and "missing" not in dict(ranked)


def test_rank_with_no_skills_or_no_words():
    assert search.rank("merge", []) == []
    assert search.rank("the and of", catalog.parse_index(INDEX)) == []


# --- decisions ---

def test_session_level():
    s, _ = settings(min_score=3, platform_levels={"whatsapp": "full"})
    assert decide.session_level(s, platform="whatsapp", top=0) == ("full", "platform:whatsapp")
    assert decide.session_level(s, platform="cli", top=3) == ("lean", "match")
    assert decide.session_level(s, platform="cli", top=2.9) == ("none", "no_match")


def test_tip_takes_the_close_runners_up_only():
    s, _ = settings(min_score=2, max_tips=2)
    assert decide.tip(s, [("a", 10.0), ("b", 6.0), ("c", 5.5)]) == ["a", "b"]
    assert decide.tip(s, [("a", 10.0), ("b", 4.0)]) == ["a"]
    assert decide.tip(s, [("a", 1.9)]) == []


def test_chosen_for_session_puts_pins_in_the_index_first():
    s, _ = settings(min_score=1, pin=["ascii-art", "gone"])
    assert decide.chosen_for_session(s, [("x", 5.0)], {"ascii-art", "x"}) == ("ascii-art", "x")


def test_arm_follows_the_share_and_the_end_date():
    assert decide.arm(settings()[0], "s") == "normal"
    assert decide.arm(settings(**{"ablation.share": 1.0})[0], "s") == "ablation"
    ended, _ = settings(**{"ablation.share": 1.0, "ablation.until": "2020-01-01"})
    assert decide.arm(ended, "s") == "normal"
    half, _ = settings(**{"ablation.share": 0.5})
    arms = [decide.arm(half, f"session-{i}") for i in range(400)]
    assert 140 < arms.count("ablation") < 260 and decide.arm(half, "session-7") == arms[7]


@pytest.mark.parametrize("text", ["No, that is wrong", "Actually I meant the other file", "nee, ik bedoelde iets anders",
                                  "Dat klopt niet", "  wrong again"])
def test_correction(text):
    assert decide.is_correction(text)


@pytest.mark.parametrize("text", ["Nobody answered", "Now merge it", "Notes for today", ""])
def test_no_correction(text):
    assert not decide.is_correction(text)


def test_repeat():
    assert decide.is_repeat("merge the open pull request now", "merge the open pull request now please")
    assert not decide.is_repeat("merge the open pull request", "draw a cat")
    assert not decide.is_repeat(None, "merge it") and not decide.is_repeat("hi", "hi")


def test_session_key_is_short_and_stable():
    key = decide.session_key("20261124_abc")
    assert key == decide.session_key("20261124_abc") and len(key) == 12 and "abc" not in key


def test_hint():
    assert decide.hint(["a"]) == "<skill-hint>Relevant skill: a. Load one with skill_view if you need it.</skill-hint>"
    assert "skills: a, b." in decide.hint(["a", "b"])


# --- request variants ---

def request(user="Merge my PR\n\n<skill-hint>Relevant skill: github-pr-workflow.</skill-hint>"):
    return {"model": "m", "stream": True, "max_tokens": 500, "tool_choice": "auto", "parallel_tool_calls": True,
            "messages": [{"role": "system", "content": INDEX}, {"role": "user", "content": user},
                         {"role": "assistant", "content": "ok"}],
            "tools": copy.deepcopy(TOOLS)}


def names(req):
    return [shape.tool_name(t) for t in req.get("tools") or []]


def test_full_is_the_request_itself():
    req = request()
    assert shape.variant(req, "full", skills=("x",), local_tools=("terminal",)) is req


def test_lean_keeps_local_tools_skill_view_and_the_chosen_skills():
    req = request()
    before = copy.deepcopy(req)
    out = shape.variant(req, "lean", skills=("github-pr-workflow",), local_tools=("terminal", "read_file", "clarify"))
    assert req == before  # the original is left alone
    assert names(out) == ["terminal", "read_file", "skill_view"]
    assert [s.name for s in catalog.parse_index(shape.system_text(out))] == ["github-pr-workflow"]
    assert "<skill-hint>" not in out["messages"][1]["content"] and out["messages"][2] == req["messages"][2]
    assert out["stream"] is True and out["max_tokens"] == 500 and out["tool_choice"] == "auto"


def test_none_has_local_tools_and_no_skills():
    out = shape.variant(request(), "none", skills=("github-pr-workflow",), local_tools=("terminal",))
    assert names(out) == ["terminal"] and "skill_view" not in shape.system_text(out)
    assert catalog.parse_index(shape.system_text(out)) is None


def test_bare_has_no_tool_keys():
    out = shape.variant(request(), "bare", local_tools=("terminal",))
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(out)


def test_variants_are_the_same_every_time():
    kw = {"skills": ("ascii-art",), "local_tools": ("terminal",)}
    assert json.dumps(shape.variant(request(), "lean", **kw)) == json.dumps(shape.variant(request(), "lean", **kw))


def test_fit_steps_down_until_the_request_fits():
    kw = {"skills": ("github-pr-workflow",), "local_tools": ("terminal",)}
    size = {name: len(json.dumps(shape.variant(request(), name, **kw))) for name in shape.ORDER}
    assert size["full"] > size["lean"] > size["none"] > size["bare"]
    assert shape.fit(request(), "full", lambda r: len(json.dumps(r)) <= size["none"], **kw)[0] == "none"
    assert shape.fit(request(), "lean", lambda r: True, **kw)[0] == "lean"
    assert shape.fit(request(), "full", lambda r: False, **kw)[0] == "bare"


def test_without_skills_drops_the_skill_tools_and_the_index():
    out = shape.without_skills(request())
    assert names(out) == ["terminal", "read_file", "write_file", "web_search", "browser_navigate", "image_generate"]
    assert "skill_view" not in shape.system_text(out) and "<skill-hint>" not in out["messages"][1]["content"]


def test_without_skills_reads_each_format_hermes_sends():
    tool_names = [shape.tool_name(t) for t in TOOLS]
    cache = {"cachePoint": {"type": "default"}}
    formats = {
        "anthropic": {"system": [{"type": "text", "text": INDEX, "cache_control": {"type": "ephemeral"}}],
                      "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                      "tools": [{"name": n, "input_schema": {}} for n in tool_names]},
        "responses": {"instructions": INDEX, "input": [{"role": "user", "content": "hi"}],
                      "tools": [{"type": "function", "name": n} for n in tool_names]},
        # GPT-6.2 and newer: Hermes moves the system prompt into a developer item at the start of input
        "responses_developer": {"input": [{"type": "message", "role": "developer",
                                           "content": [{"type": "input_text", "text": INDEX}]},
                                          {"type": "message", "role": "user",
                                           "content": [{"type": "input_text", "text": "hi"}]}],
                                "tools": [{"type": "function", "name": n} for n in tool_names]},
        "bedrock": {"system": [{"text": INDEX}, cache], "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                    "toolConfig": {"tools": [{"toolSpec": {"name": n}} for n in tool_names] + [cache]}},
        "developer": {"messages": [{"role": "developer", "content": INDEX}, {"role": "user", "content": "hi"}],
                      "tools": copy.deepcopy(TOOLS)},
    }
    for name, req in formats.items():
        out = json.dumps(shape.without_skills(req))
        assert "skill_view" not in out and "<available_skills>" not in out, name
        assert "terminal" in out and "You are Hermes" in out, name
        assert set(shape.without_skills(req)) == set(req), name  # the Responses API rejects a `messages` key
    assert shape.without_skills(formats["anthropic"])["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert shape.without_skills(formats["bedrock"])["toolConfig"]["tools"][-1] == cache


# --- the log ---

def test_log_round_trip_and_private_file(tmp_path):
    path = tmp_path / "data" / log.NAME
    log.write(path, {"turn": 1})
    log.write(path, {"turn": 2})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("not json\n[1]\n")
    assert log.read(path) == [{"turn": 1}, {"turn": 2}]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_log_keeps_one_older_file(tmp_path, monkeypatch):
    monkeypatch.setattr(log, "MAX_BYTES", 40)
    path = tmp_path / log.NAME
    for i in range(6):
        log.write(path, {"turn": i, "pad": "x" * 10})
    assert path.with_name(log.NAME + ".1").exists()
    turns = [r["turn"] for r in log.read(path)]
    assert turns == sorted(turns) and turns[-1] == 5 and len(turns) < 6


def test_reading_a_missing_log_is_empty(tmp_path):
    assert log.read(tmp_path / log.NAME) == []


# --- the report ---

NOW = 1_800_000_000.0


def rec(t, **kw):
    base = {"time": NOW - t * DAY, "session": "a", "turn": 1, "platform": "cli", "level": "lean", "arm": "normal",
            "tip": [], "tip_given": False, "used_skills": [], "answered_by": ["m1"], "outcome": "completed",
            "tool_errors": 0, "tokens_in": 100, "tokens_out": 10}
    return {**base, **kw}


def test_report_without_records():
    assert "logged no turns in the last 30 days" in report.render([], now=NOW)


def test_report_counts_levels_tips_and_loads():
    records = [rec(1, session="a", tip=["x"], tip_given=True, used_skills=["x"]),
               rec(1, session="b", level="none", used_skills=["y"]),
               rec(40, session="old", used_skills=["z"])]
    out = report.render(records, now=NOW)
    assert "2 sessions, 2 turns" in out and "lean 1, none 1" in out
    assert "in 1 of 1 the model loaded a tipped skill. Tips sent to the model: 1." in out
    assert "Skills loaded: in 2 turns, 1 of them without a matching tip." in out and "z" not in out


def test_report_proposes_unused_skills_only_with_enough_history():
    catalogs = {"cli": ["x", "y", "never"]}
    short = [rec(1, used_skills=["x"]), rec(3, used_skills=["y"])]
    assert "this needs 14 days or more" in report.render(short, catalogs=catalogs, now=NOW)
    long = [rec(1, used_skills=["x"]), rec(20, used_skills=["y"])]
    out = report.render(long, catalogs=catalogs, now=NOW)
    assert "Proposal: these 1 skills were not loaded" in out and out.rstrip().endswith("never")


def test_report_compares_the_ablation_arm():
    records = [rec(1, session="a"), rec(1, session="b", arm="ablation", level="none", tool_errors=2, outcome="failed")]
    out = report.render(records, now=NOW)
    assert "Ablation comparison" in out
    row = next(line for line in out.splitlines() if line.strip().startswith("ablation"))
    assert row.split()[1:4] == ["1", "1", "2.00"]
    assert "Proposal: run an ablation week" not in out


def test_report_proposes_an_ablation_week_for_a_new_model():
    records = [rec(30, answered_by=["m1"]), rec(2, session="b", answered_by=["m2"])]
    out = report.render(records, now=NOW)
    assert "Proposal: run an ablation week (new model(s) since 7 days: m2)" in out
    assert "then restart Hermes or the gateway" in out and "ablation: {share: 0.25, until: " in out
    assert "Proposal: run an ablation week" not in report.render([rec(2)], now=NOW)
