"""Pin a release in hermes-pack.yaml: rewrite the ref of every entry of one repository."""

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ENTRY = re.compile(r"^(\s*-\s*repo:\s*)(\S+)")
_REF = re.compile(r"^(\s*ref:\s*)([0-9a-f]{40})(\s.*)?$")


def bump_pack_refs(pack_path: Path, repo: str, sha: str) -> int:
    """Rewrite the ref lines as text, so comments and layout stay. Returns the number of entries changed."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("sha must be 40 lowercase hex characters")
    current, count, out = None, 0, []
    for line in pack_path.read_text().splitlines(keepends=True):
        entry, ref = _ENTRY.match(line), _REF.match(line.rstrip("\n"))
        if entry:
            current = entry.group(2)
        elif ref and current == repo:
            line = f"{ref.group(1)}{sha}{ref.group(3) or ''}\n"
            count += 1
        out.append(line)
    pack_path.write_text("".join(out))
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", help="commit to pin (default: git rev-parse HEAD)")
    parser.add_argument("--repo", default="Lil-Clanky/polytropos")
    args = parser.parse_args(argv)
    sha = args.sha or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    count = bump_pack_refs(ROOT / "hermes-pack.yaml", args.repo, sha)
    print(f"pinned {count} entries of {args.repo} to {sha}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
