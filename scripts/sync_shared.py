"""Copy shared/polytropos_shared into each plugin's _shared folder, and versions.lock into the general plugin."""

import os
import sys
import tempfile
import time
from pathlib import Path

PLUGINS = ("polytropos", "polytropos_router")
MODE = 0o644
TEMP_GRACE = 60  # seconds: a younger temp file may belong to a sync running right now


def _pairs(root: Path):
    for src in sorted((root / "shared" / "polytropos_shared").glob("*.py")):
        for plugin in PLUGINS:
            yield src, root / "plugins" / plugin / "_shared" / src.name
    # A pack install copies only the plugin folders, so the general plugin carries its own copy of the lock.
    yield root / "versions.lock", root / "plugins" / "polytropos" / "versions.lock"


def _stale(root: Path):
    """Top-level files in a managed _shared folder that sync does not produce (folders are left alone)."""
    wanted = {dst for _, dst in _pairs(root)}
    for plugin in PLUGINS:
        folder = root / "plugins" / plugin / "_shared"
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if path in wanted or path.is_dir() and not path.is_symlink():
                continue
            if path.name.startswith(".") and time.time() - path.lstat().st_mtime < TEMP_GRACE:
                continue
            yield path


def _mode_ok(path: Path) -> bool:
    return path.stat().st_mode & 0o777 == MODE


def sync(root: Path) -> list[Path]:
    """Write changed copies only, atomically: parallel test runs sync at the same time.

    Also deletes stale files inside the managed _shared folders and repairs wrong modes.
    """
    for path in _stale(root):
        path.unlink(missing_ok=True)
    written = []
    for src, dst in _pairs(root):
        data = src.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            if not _mode_ok(dst):
                os.chmod(dst, MODE)
                written.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, MODE)
            os.replace(tmp, dst)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written.append(dst)
    return written


def check(root: Path) -> list[str]:
    bad = [
        str(dst) for src, dst in _pairs(root)
        if not dst.exists() or dst.read_bytes() != src.read_bytes() or not _mode_ok(dst)
    ]
    return bad + [str(path) for path in _stale(root)]


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    if "--check" in sys.argv:
        bad = check(root)
        for path in bad:
            print(f"differs: {path}")
        sys.exit(1 if bad else 0)
    for path in sync(root):
        print(f"wrote {path}")
