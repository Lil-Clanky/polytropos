"""A system prompt and tools shaped like the ones Hermes sends (agent/prompt_builder.py), for the Polymetis tests."""

INDEX = (
    "You are Hermes, an assistant.\n\n"
    "## Skills\n"
    "Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, "
    "you MUST load it with skill_view(name) and follow its instructions. Err on the side of loading.\n"
    "If a skill has issues, fix it with skill_manage(action='patch').\n\n"
    "<available_skills>\n"
    "  github:\n"
    "    - github-pr-workflow: Open, review and merge pull requests with gh\n"
    "    - github-issues: Triage and label GitHub issues\n"
    "  - ascii-art: Draw pictures out of text characters\n"
    "  media [names only]: gif-search, youtube-content\n"
    "</available_skills>\n\n"
    "Only proceed without loading a skill if genuinely none are relevant to the task.\n"
    "(Categories marked [names only] are outside the current coding context, so their descriptions are "
    "omitted — the skills work normally and load with skill_view(name) as usual.)\n\n"
    "## Memory\n"
    "The user likes short answers. The `hermes-agent` skill has the commands — load it with "
    "skill_view(name='hermes-agent') before configuring Hermes. The docs are the reference."
)

ONESHOT = (
    "You are Hermes.\n\n"
    "## Skills\n"
    "Scan the skills below and load one with skill_view(name) only when it carries domain knowledge you lack "
    "for THIS task.\n"
    "\n<available_skills>\n"
    "  - ascii-art: Draw pictures out of text characters\n"
    "</available_skills>\n\n"
    "## Memory\nShort answers."
)


def tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": f"The {name} tool.",
                                             "parameters": {"type": "object", "properties": {}}}}


TOOLS = [tool(n) for n in ("terminal", "read_file", "write_file", "web_search", "browser_navigate",
                           "skills_list", "skill_view", "skill_manage", "image_generate")]
