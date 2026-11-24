"""Probe how small local models behave when their tools or skill tools are taken away.

Answers two questions before the skill router trims anything for local models:
  V2: do small models type tool calls as plain text when the request carries no tools?
  V3: does a skills index without the skill_view tool make them call a tool that is not there?

Usage: python spikes/skill_exposure_probe.py SKILLS_JSON OUT_JSONL [MODEL ...]
SKILLS_JSON is a list of {"name", "description", "category"} (the bundled Hermes skills).
Talks to the Ollama at OLLAMA_HOST (default http://127.0.0.1:11434) with the stdlib only.
Each model stays loaded for two minutes after its last request; two models took about 20 minutes.
Results of 2026-11-24: docs/spikes/2026-11-24-skill-exposure.md.
"""

import json
import os
import re
import sys
import time
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
RUNS = 3
NUM_CTX = 8192

# The wording of the index block follows Hermes agent/prompt_builder.py (2506f5b0, MIT, Nous Research).
SKILLS_HEADER = (
    "## Skills\nBefore replying, scan the skills below. If a skill matches or is even partially relevant to your "
    "task, you MUST load it with skill_view(name) and follow its instructions. Err on the side of loading.\n"
    "<available_skills>\n")
SKILLS_FOOTER = "\n</available_skills>\n\nOnly proceed without loading a skill if genuinely none are relevant to the task."
IDENTITY = "You are Hermes, a helpful assistant running on the user's computer. Today is 2026-11-24."


def _tool(tool_name, description, params):
    return {"type": "function", "function": {
        "name": tool_name, "description": description,
        "parameters": {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in params.items()},
                       "required": list(params)}}}


TERMINAL = _tool("terminal", "Run a shell command and return its output.", {"command": "The command to run"})
READ_FILE = _tool("read_file", "Read a text file.", {"path": "Path of the file"})
WEB_SEARCH = _tool("web_search", "Search the web and return the top results.", {"query": "The search query"})
SKILL_VIEW = _tool("skill_view", "Load a skill's instructions by name.", {"name": "The skill name"})

PROMPTS = [
    "Which files are in my Downloads folder?",
    "What is the weather in Amsterdam today?",
    "Open my pull request on GitHub and tell me if CI passed.",
    "Merge the two PDFs report-a.pdf and report-b.pdf into one file.",
    "Find recent arXiv papers about tool retrieval for LLM agents.",
    "My Python script crashes with KeyError: 'id'. Help me find the cause.",
    "Add a note to my Obsidian vault: call the plumber on Friday.",
    "Plan my week: what is still open from last week?",
]

CONDITIONS = {
    "control": (True, [TERMINAL, READ_FILE, WEB_SEARCH, SKILL_VIEW]),  # index and every tool
    "no_tools": (True, []),                                              # V2: index, no tools at all
    "index_no_skill_tool": (True, [TERMINAL, READ_FILE, WEB_SEARCH]),    # V3: index, skill_view gone
    "bare": (False, []),                                                 # no index, no tools
}

TEXT_CALL = re.compile(r'<tool_call>|"name"\s*:\s*"|"arguments"\s*:|\b(skill_view|terminal|read_file|web_search)\s*\(',
                       re.I)


def system_prompt(skills, with_index):
    if not with_index:
        return IDENTITY
    by_cat = {}
    for s in skills:
        by_cat.setdefault(s.get("category") or "general", []).append(s)
    lines = []
    for cat in sorted(by_cat):
        rows = sorted(by_cat[cat], key=lambda s: s["name"])
        indent = "  " if len(rows) == 1 else "    "
        if len(rows) > 1:
            lines.append(f"  {cat}:")
        lines += [f"{indent}- {s['name']}: {s['description'][:60]}" for s in rows]
    return IDENTITY + "\n\n" + SKILLS_HEADER + "\n".join(lines) + SKILLS_FOOTER


def chat(model, system, prompt, tools, seed):
    body = {"model": model, "stream": False, "keep_alive": "2m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"num_ctx": NUM_CTX, "temperature": 0.7, "seed": seed}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    return data, time.monotonic() - started


def classify(data, tools):
    msg = data.get("message") or {}
    offered = {t["function"]["name"] for t in tools}
    calls = [c.get("function", {}).get("name", "") for c in msg.get("tool_calls") or []]
    text = msg.get("content") or ""
    return {"calls": calls, "unknown_calls": [c for c in calls if c not in offered],
            "text_call": bool(TEXT_CALL.search(text)), "mentions_skill_view": "skill_view" in text,
            "prompt_tokens": data.get("prompt_eval_count"), "chars": len(text)}


def main():
    skills = json.load(open(sys.argv[1], encoding="utf-8"))
    out_path, models = sys.argv[2], sys.argv[3:] or ["llama3.2:3b"]
    with open(out_path, "a", encoding="utf-8") as out:
        for model in models:
            for cond, (with_index, tools) in CONDITIONS.items():
                system = system_prompt(skills, with_index)
                for p_i, prompt in enumerate(PROMPTS):
                    for run in range(RUNS):
                        try:
                            data, secs = chat(model, system, prompt, tools, seed=run + 1)
                            row = classify(data, tools)
                        except Exception as e:  # keep going: one failed call is a data point too
                            row, secs = {"error": type(e).__name__}, 0.0
                        row.update(model=model, condition=cond, prompt=p_i, run=run, seconds=round(secs, 2))
                        out.write(json.dumps(row) + "\n")
                        out.flush()


if __name__ == "__main__":
    main()
