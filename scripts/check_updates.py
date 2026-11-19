"""Check the pinned programs against their upstream releases and print the updates as JSON."""

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GITHUB = "https://api.github.com/repos/"
Fetch = Callable[[str], bytes]

# component -> (lock section, GitHub repo or None for PyPI, key holding the current value)
COMPONENTS = {
    "freellmapi": ("freellmapi", "tashfeenahmed/freellmapi", "tag"),
    "rtk": ("rtk", "rtk-ai/rtk", "version"),
    "graphify": ("graphify", None, "version"),
    "ast-grep": ("ast-grep", None, "version"),
    "ponytail": ("plugins.ponytail", "DietrichGebert/ponytail", "tag"),
    "superpowers": ("plugins.superpowers", "obra/superpowers", "tag"),
}
PACK_REPOS = {"ponytail": "DietrichGebert/ponytail", "superpowers": "obra/superpowers"}


def http_fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "polytropos-check"}), timeout=30) as r:
        return r.read()


def parse_version(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def newer(a: str, b: str) -> bool:
    return parse_version(a) > parse_version(b)


def latest_tag(repo: str, fetch: Fetch) -> str:
    try:
        return json.loads(fetch(f"{GITHUB}{repo}/releases/latest"))["tag_name"]
    except (OSError, KeyError, ValueError):
        names = [t["name"] for t in json.loads(fetch(f"{GITHUB}{repo}/tags")) if parse_version(t["name"])]
        if not names:
            raise ValueError(f"{repo} has no version tags")
        return max(names, key=parse_version)


def pypi_version(package: str, fetch: Fetch) -> str:
    return json.loads(fetch(f"https://pypi.org/pypi/{package}/json"))["info"]["version"]


def update_lock(path: Path, section: str, values: dict[str, str]) -> bool:
    """Rewrite `key = "value"` lines inside one section only. Returns whether the file changed."""
    out, inside = [], False
    for line in path.read_text().splitlines(keepends=True):
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            inside = header.group(1) == section
        elif inside:
            m = re.match(r'^(\s*(\w[\w-]*)\s*=\s*)"[^"]*"', line)
            if m and m.group(2) in values:
                line = f'{m.group(1)}"{values[m.group(2)]}"{line[m.end():]}'
        out.append(line)
    new = "".join(out)
    if new == path.read_text():
        return False
    path.write_text(new)
    return True


def find_updates(lock_path: Path, fetch: Fetch) -> list[dict[str, str]]:
    lock = tomllib.loads(lock_path.read_text())
    found = []
    for name, (section, repo, key) in COMPONENTS.items():
        table = lock
        for part in section.split("."):
            table = table[part]
        current = table[key]
        latest = latest_tag(repo, fetch) if repo else pypi_version(table["package"], fetch)
        if newer(latest, current):
            found.append({"component": name, "current": current, "latest": latest})
    return found


def apply_update(lock_path: Path, pack_path: Path, update: dict[str, str], fetch: Fetch) -> None:
    """Write one update from find_updates into versions.lock (and the pack file for plugins)."""
    name, latest = update["component"], update["latest"]
    section, repo, key = COMPONENTS[name]
    values = {key: latest}
    if name in PACK_REPOS or name == "freellmapi":
        # the commits endpoint peels annotated tags
        sha = json.loads(fetch(f"{GITHUB}{repo}/commits/{latest}"))["sha"]
        values["commit"] = sha
        if name in PACK_REPOS:
            release.bump_pack_refs(pack_path, PACK_REPOS[name], sha)
    update_lock(lock_path, section, values)
    if name == "rtk":
        # Each asset keeps its target name; its url and sha256 follow the new release.
        sums = fetch(f"https://github.com/{repo}/releases/download/{latest}/checksums.txt").decode()
        digests = {n: d for d, n in (l.split() for l in sums.splitlines() if l.strip())}
        for target, asset in tomllib.loads(lock_path.read_text())["rtk"]["assets"].items():
            file = asset["url"].rsplit("/", 1)[1]
            update_lock(lock_path, f"rtk.assets.{target}", {
                "url": f"https://github.com/{repo}/releases/download/{latest}/{file}",
                "sha256": digests[file],
            })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "versions.lock")
    parser.add_argument("--apply", metavar="COMPONENT", help="write this component's update into versions.lock")
    args = parser.parse_args(argv)
    updates = find_updates(args.lock, http_fetch)
    if args.apply:
        update = next((u for u in updates if u["component"] == args.apply), None)
        if update is None:
            print(f"no update for {args.apply}", file=sys.stderr)
            return 1
        apply_update(args.lock, ROOT / "hermes-pack.yaml", update, http_fetch)
        updates = [update]
    print(json.dumps(updates, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
