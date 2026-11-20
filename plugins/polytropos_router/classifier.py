import re
from typing import Any, Callable

from .config import Slot

CLASSIFIER_PROMPT = (
    "You are a model-routing classifier. Read the conversation snippet and "
    "decide which execution tier the NEXT reply needs. Answer with exactly "
    'one word: "simple" or "complex". No punctuation, no explanation.\n\n'
    "simple = casual conversation, chit-chat, quick questions, short "
    "texting-style replies, small factual lookups, image descriptions, "
    "greetings, reminders about trivial things.\n"
    "complex = coding, debugging, multi-step tasks, heavy tool use (files, "
    "terminal, browser, scheduling, web research), long documents, planning, "
    "anything likely to take many steps or careful reasoning."
)


def classify(create: Callable[..., Any], slot: Slot, user_text: str, *,
             max_tokens: int, timeout_s: float) -> str | None:
    if len(user_text) > 2000:  # the classifier sees the start and the end of a long message
        user_text = user_text[:1000] + "\n" + user_text[-1000:]
    try:
        reply = create(
            model=slot.model,
            messages=[{"role": "system", "content": CLASSIFIER_PROMPT},
                      {"role": "user", "content": user_text}],
            max_tokens=max_tokens, temperature=0, timeout=timeout_s,
        )
        words = set(re.findall(r"simple|complex", (reply.choices[0].message.content or "").lower()))
    except Exception:
        return None
    return words.pop() if len(words) == 1 else None
