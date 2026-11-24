"""Hermes's skill index: read it from a system prompt, and cut it down for a smaller request.

Hermes writes the index as an ``<available_skills>`` block, one ``- name: description`` row per
skill, under ``category:`` headers, or a ``category [names only]: a, b`` row for categories outside
the coding context (agent/prompt_builder.py, ``_render_skills_index``).
"""

import re
from dataclasses import dataclass

OPEN, CLOSE = "<available_skills>", "</available_skills>"
HEADER = "## Skills\n"
LAST_LINE = "Only proceed without loading a skill if genuinely none are relevant to the task."
HINT = re.compile(r"\n*<skill-hint>.*?</skill-hint>", re.S)
# shortcut: drops whole sentences that point at skill_view(, which matches Hermes's wording today;
# a sentence with a dot inside the call would be cut short, revisit when the wording changes
POINTER = re.compile(r"[^.\n]*\bskill_view\([^)]*\)[^.\n]*\.?[ \t]?")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str = ""
    category: str = ""


def text_of(content) -> str:
    """The text of a message content: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _block(text: str) -> tuple[int, int] | None:
    start = text.find(OPEN)
    end = text.find(CLOSE, start)
    return (start, end + len(CLOSE)) if start >= 0 and end >= 0 else None


def parse_index(system_text: str) -> list[Skill] | None:
    """The skills in the index, or None when the prompt has no index."""
    span = _block(system_text or "")
    if span is None:
        return None
    body = system_text[span[0] + len(OPEN):span[1] - len(CLOSE)]
    skills, category = [], ""
    for line in body.splitlines():
        row = line.strip()
        if row.startswith("- "):
            name, _, desc = row[2:].partition(": ")  # qualified names such as plugin:skill have no space
            nested = line.startswith("    ")  # a one-skill category is a top-level row of its own
            skills.append(Skill(name.strip(), desc.strip(), category if nested else ""))
        elif row.endswith(":") and row[:-1].strip():
            category = row[:-1].strip()
        elif " [names only]: " in row:
            category, _, names = row.partition(" [names only]: ")
            skills += [Skill(n.strip(), "", category.strip()) for n in names.split(",") if n.strip()]
            category = ""
    return list({s.name: s for s in skills}.values())


def _without_block(text: str) -> str:
    span = _block(text)
    if span is None:
        return text
    start, end = span
    header = text.rfind(HEADER, 0, start)
    if header >= 0 and "\n## " not in text[header + len(HEADER):start]:
        start = header
    tail = text[end:]
    stripped = tail.lstrip("\n")
    if stripped.startswith(LAST_LINE):
        end += len(tail) - len(stripped) + len(LAST_LINE)
    # Hermes may follow the block with notes in parentheses about hidden or unloadable skills; they end
    # with ".)" and can hold parentheses of their own, such as skill_view(name)
    while (rest := text[end:]).startswith("\n("):
        cut = rest.find(".)", 2)
        end += cut + 2 if cut >= 0 else len(rest)
    return (text[:start].rstrip("\n") + "\n\n" + text[end:].lstrip("\n")).strip("\n")


def strip_skills(text: str) -> str:
    """The system prompt without the skill index and without the sentences that point at skill_view."""
    return POINTER.sub("", _without_block(text))


def keep_skills(text: str, names) -> str:
    """The system prompt with an index of only the named skills, or without an index when none is left."""
    wanted = set(names)
    skills = [s for s in parse_index(text) or [] if s.name in wanted]
    span = _block(text)
    if span is None or not skills:
        return strip_skills(text)
    rows = "\n".join(f"  - {s.name}: {s.description}" if s.description else f"  - {s.name}" for s in skills)
    return text[:span[0]] + OPEN + "\n" + rows + "\n" + CLOSE + text[span[1]:]


def strip_hints(text: str) -> str:
    return HINT.sub("", text)
