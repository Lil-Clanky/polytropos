import json


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def broken_tool_calls(tool_calls: list, tools: list[dict] | None) -> list[str]:
    required = {_get(t["function"] if "function" in t else t, "name"):
                (_get(t.get("function", t), "parameters") or {}).get("required") or []
                for t in tools or []}
    reasons = []
    for call in tool_calls:
        fn = _get(call, "function")
        name = _get(fn, "name") or ""
        if name not in required:
            reasons.append(f"unknown tool {name}")
            continue
        try:
            args = json.loads(_get(fn, "arguments") or "{}")
        except (TypeError, ValueError):
            args = None
        if not isinstance(args, dict):
            reasons.append(f"arguments of {name} are not a JSON object")
            continue
        reasons += [f"{name} is missing required {key}" for key in required[name] if key not in args]
    return reasons
