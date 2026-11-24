"""Smaller requests: the fixed variants a local model gets, and a request without skills for an ablation.

Every function returns a new request and leaves the one it got alone. The same session and settings
always give the same request, so a local model's prompt cache holds from one turn to the next.
"""

from .catalog import is_text_part, keep_skills, strip_hints, strip_skills, text_of

SKILL_TOOLS = frozenset({"skills_list", "skill_view", "skill_manage"})
ORDER = ("full", "lean", "none", "bare")  # richest first; a request that does not fit steps down
SYSTEM_ROLES = ("system", "developer")  # Hermes sends GPT-5 and Codex models the system prompt as developer
_TOOL_KEYS = ("tools", "tool_choice", "parallel_tool_calls")


def tool_name(tool) -> str:
    """A tool definition's name: under ``function`` (chat completions) or ``toolSpec`` (Bedrock), or at the
    top (Anthropic, the Responses API)."""
    if not isinstance(tool, dict):
        return ""
    inner = tool.get("function") or tool.get("toolSpec")
    return str((inner if isinstance(inner, dict) else tool).get("name") or "")


def _edit_text(content, edit):
    if isinstance(content, str):
        return edit(content)
    if isinstance(content, list):
        return [{**p, "text": edit(p["text"])} if is_text_part(p) else p for p in content]
    return content


def _messages(messages, system_edit) -> list:
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
        elif m.get("role") in SYSTEM_ROLES:
            out.append({**m, "content": _edit_text(m.get("content"), system_edit)})
        elif m.get("role") == "user":
            out.append({**m, "content": _edit_text(m.get("content"), strip_hints)})
        else:
            out.append(m)
    return out


def _with_tools(request: dict, tools: list) -> dict:
    out = {k: v for k, v in request.items() if k not in _TOOL_KEYS or (tools and k != "tools")}
    if tools:
        out["tools"] = tools
    return out


def variant(request: dict, level: str, *, skills=(), local_tools=()) -> dict:
    """The request a local model gets at ``level``: ``full`` is the request as it is, ``lean`` keeps
    ``local_tools``, ``skill_view`` and an index of ``skills``, ``none`` keeps ``local_tools`` and no
    index, ``bare`` has no tools and no index."""
    if level == "full":
        return request
    keep = set(local_tools) | ({"skill_view"} if level == "lean" and skills else set())
    tools = [] if level == "bare" else [t for t in request.get("tools") or [] if tool_name(t) in keep]
    edit = (lambda text: keep_skills(text, skills)) if level == "lean" and skills else strip_skills
    return _with_tools({**request, "messages": _messages(request.get("messages"), edit)}, tools)


def without_skills(request: dict) -> dict:
    """The request with every tool except the skill tools, and without the index or its pointers, in each
    format Hermes sends: the system prompt as a ``system`` or ``developer`` message (chat completions), as
    a top-level ``system`` (Anthropic, Bedrock), or as ``instructions`` or a developer item in ``input``
    (the Responses API), and the tools as ``tools`` or Bedrock's ``toolConfig``. It changes only the keys
    the request has."""
    out = dict(request)
    for key in ("messages", "input"):
        if isinstance(out.get(key), list):
            out[key] = _messages(out[key], strip_skills)
    for key in ("system", "instructions"):
        if key in out:
            out[key] = _edit_text(out[key], strip_skills)
    config = out.get("toolConfig")
    if isinstance(config, dict) and isinstance(config.get("tools"), list):
        out["toolConfig"] = {**config, "tools": [t for t in config["tools"] if tool_name(t) not in SKILL_TOOLS]}
    if "tools" not in out:
        return out
    return _with_tools(out, [t for t in out["tools"] or [] if tool_name(t) not in SKILL_TOOLS])


def fit(request: dict, level: str, fits, **kw) -> tuple[str, dict]:
    """The richest variant from ``level`` down for which ``fits`` holds; the router passes ``fits``, so
    that both count tokens the same way. ``bare`` is the last step and is returned even when it does
    not fit, for the router to judge."""
    for name in ORDER[ORDER.index(level):]:
        shaped = variant(request, name, **kw)
        if name == "bare" or fits(shaped):
            return name, shaped
    raise AssertionError("unreachable: bare always returns")  # pragma: no cover


def system_text(request: dict) -> str:
    for m in request.get("messages") or []:
        if isinstance(m, dict) and m.get("role") in SYSTEM_ROLES:
            return text_of(m.get("content"))
    return ""
