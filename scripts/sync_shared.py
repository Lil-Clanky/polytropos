"""Copy shared/polytropos_shared into each plugin's _shared folder."""

import os
import sys
import tempfile
from pathlib import Path

PLUGINS = ("polytropos", "polytropos_router")


def _pairs(root: Path):
    for src in sorted((root / "shared" / "polytropos_shared").glob("*.py")):
        for plugin in PLUGINS:
            yield src, root / "plugins" / plugin / "_shared" / src.name


def sync(root: Path) -> list[Path]:
    """Write changed copies only, atomically: parallel test runs sync at the same time."""
    written = []
    for src, dst in _pairs(root):
        data = src.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, dst)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written.append(dst)
    return written


def check(root: Path) -> list[str]:
    return [
        str(dst) for src, dst in _pairs(root)
        if not dst.exists() or dst.read_bytes() != src.read_bytes()
    ]


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    if "--check" in sys.argv:
        bad = check(root)
        for path in bad:
            print(f"differs: {path}")
        sys.exit(1 if bad else 0)
    for path in sync(root):
        print(f"wrote {path}")
