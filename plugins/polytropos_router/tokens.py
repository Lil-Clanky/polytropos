import json

from ._shared.bridge import message_text


def estimate_tokens(messages: list, tools: list | None = None) -> int:
    flat = [{**m, "content": message_text(m.get("content"))} if isinstance(m.get("content"), list) else m
            for m in messages]
    # shortcut: character estimate, replace with a tokenizer if routing misjudges sizes
    return len(json.dumps([flat, tools or []], default=str)) // 3
