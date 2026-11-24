"""The report: counts from the decision log, and proposals. It reads and prints; it never changes a setting."""

import datetime
import time
from collections import Counter, defaultdict

DAY = 86400.0
MIN_SPAN_DAYS = 14  # proposals to disable a skill need at least this much log
ABLATION_EVERY_DAYS = 90
NEW_MODEL_DAYS = 7


def _span_days(records) -> float:
    times = [r.get("time", 0) for r in records]
    return (max(times) - min(times)) / DAY if times else 0.0


def _share(part: int, whole: int) -> str:
    return f"{part} of {whole}" if whole else "0"


def _ablation(all_records, now: float) -> list[str]:
    recent = [r for r in all_records if now - r.get("time", 0) <= ABLATION_EVERY_DAYS * DAY]
    if any(r.get("arm") == "ablation" for r in recent):
        return []
    first_seen: dict[str, float] = {}
    for r in sorted(all_records, key=lambda r: r.get("time", 0)):
        for model in r.get("answered_by") or []:
            first_seen.setdefault(model, r.get("time", 0))
    oldest = min((r.get("time", now) for r in all_records), default=now)
    new = sorted(m for m, t in first_seen.items() if now - t <= NEW_MODEL_DAYS * DAY and t - oldest > DAY)
    if not new and now - oldest < ABLATION_EVERY_DAYS * DAY:
        return []
    until = (datetime.date.fromtimestamp(now) + datetime.timedelta(days=7)).isoformat()
    why = (f"new model(s) since {NEW_MODEL_DAYS} days: {', '.join(new)}" if new
           else f"no ablation in the last {ABLATION_EVERY_DAYS} days")
    return ["", f"Proposal: run an ablation week ({why}). A quarter of new sessions then runs without skills,",
            "and this report compares both groups. Set in config.yaml, then restart Hermes or the gateway:",
            f"  plugins.entries.polymetis.settings.ablation: {{share: 0.25, until: {until}}}"]


def _compare(records) -> list[str]:
    arms = defaultdict(list)
    for r in records:
        arms[r.get("arm") or "normal"].append(r)
    if "ablation" not in arms:
        return []
    lines = ["", "Ablation comparison (per turn unless noted):",
             f"  {'group':<10} {'sessions':>8} {'turns':>6} {'tool errors':>11} {'corrections':>11} "
             f"{'repeats':>7} {'failed':>6} {'tokens':>8}"]
    for name in ("normal", "ablation"):
        rs = arms.get(name, [])
        n = len(rs) or 1
        sessions = len({r.get("session") for r in rs})
        lines.append(
            f"  {name:<10} {sessions:>8} {len(rs):>6} {sum(r.get('tool_errors') or 0 for r in rs) / n:>11.2f} "
            f"{sum(bool(r.get('correction')) for r in rs) / n:>11.2f} {sum(bool(r.get('repeat')) for r in rs) / n:>7.2f} "
            f"{sum(r.get('outcome') == 'failed' for r in rs) / n:>6.2f} "
            f"{sum((r.get('tokens_in') or 0) + (r.get('tokens_out') or 0) for r in rs) / n:>8.0f}")
    lines.append("  Few sessions give noisy numbers; compare again after a full week.")
    return lines


def render(all_records, *, days: int = 30, catalogs: dict[str, list[str]] | None = None,
           now: float | None = None) -> str:
    now = time.time() if now is None else now
    records = [r for r in all_records if now - r.get("time", 0) <= days * DAY]
    if not records:
        return (f"Polymetis logged no turns in the last {days} days. It writes one line per turn while the "
                "setting `log` is on and the plugin is enabled.")
    sessions = {}
    for r in records:
        sessions[r.get("session")] = r  # the last record of a session holds its final level
    out = [f"Polymetis report, last {days} days: {len(sessions)} sessions, {len(records)} turns."]

    levels = Counter(r.get("level") or "undecided" for r in sessions.values())
    out.append("Session levels: " + ", ".join(f"{k} {v}" for k, v in sorted(levels.items())) + ".")
    platforms = Counter(r.get("platform") or "unknown" for r in records)
    out.append("Turns per platform: " + ", ".join(f"{k} {v}" for k, v in platforms.most_common()) + ".")

    tipped = [r for r in records if r.get("tip")]
    hit = sum(bool(set(r.get("tip") or []) & set(r.get("used_skills") or [])) for r in tipped)
    loaded = [r for r in records if r.get("used_skills")]
    untipped = sum(not set(r.get("used_skills") or []) & set(r.get("tip") or []) for r in loaded)
    given = sum(bool(r.get("tip_given")) for r in records)
    out.append(f"Tips: {len(tipped)} turns had a matching skill, and in {_share(hit, len(tipped))} the model "
               f"loaded a tipped skill. Tips sent to the model: {given}.")
    out.append(f"Skills loaded: in {len(loaded)} turns, {untipped} of them without a matching tip.")

    used = Counter(name for r in records for name in r.get("used_skills") or [])
    if used:
        out.append("Most loaded skills: " + ", ".join(f"{k} {v}" for k, v in used.most_common(10)) + ".")

    local = Counter(r.get("local") for r in records if r.get("local"))
    if local:
        misses = sum(1 for r in records if r.get("local") and set(r.get("tip") or []) - set(r.get("chosen") or []))
        out.append("Requests prepared for a local model: " + ", ".join(f"{k} {v}" for k, v in local.most_common())
                   + f". Turns whose best skill was not in the local set: {misses}.")

    outcomes = Counter(r.get("outcome") or "unknown" for r in records)
    errors = sum(r.get("tool_errors") or 0 for r in records)
    corrections = sum(bool(r.get("correction")) for r in records)
    repeats = sum(bool(r.get("repeat")) for r in records)
    out.append("Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items()))
               + f". Tool errors: {errors}. Messages that read as a correction: {corrections}, as a repeat: {repeats}.")

    names = sorted({n for names in (catalogs or {}).values() for n in names})
    if names:
        if _span_days(records) < MIN_SPAN_DAYS:
            out.append(f"Unused skills: the log covers {_span_days(records):.0f} days; this needs "
                       f"{MIN_SPAN_DAYS} days or more.")
        else:
            unused = [n for n in names if n not in used]
            if unused:
                out += ["", f"Proposal: these {len(unused)} skills were not loaded in the last {days} days. Hermes "
                        "lists every enabled skill in each prompt. To leave one out, add it to `skills.disabled` "
                        "in config.yaml:", "  " + ", ".join(unused)]
    out += _compare(records)
    out += _ablation(all_records, now)
    return "\n".join(out)
