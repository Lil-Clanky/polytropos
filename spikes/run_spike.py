"""Run one spike turn in a sandbox: ``python spikes/run_spike.py ROOT PLACEMENT``.

PLACEMENT is ``flat`` (``plugins/polytropos-spike/``), ``nested``
(``plugins/model-providers/polytropos-spike/``), ``nokey`` (flat, no ``.env``) or ``pack`` (both
spike plugins installed by ``hermes plugins pack install`` from a local git repository, config
seeded by the pack). The fake server scripts a ``write_file`` tool call, then a text reply.
Evidence lands in ROOT: ``spike-<placement>.jsonl`` (plugin log), ``run-<placement>.json`` (exit
code, output, fake requests) and, for ``pack``, ``pack-install.txt`` (terminal transcript).
"""

import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "tests")]
import hermes_sandbox  # noqa: E402
from fake_openai import FakeOpenAI  # noqa: E402

PROVIDER, PROBE = REPO / "spikes" / "provider_min", REPO / "spikes" / "hook_probe"
KEYS = "POLYTROPOS_SPIKE_KEY=spike-key-1\nPOLYTROPOS_SPIKE_OTHER=spike-other-2\n"
MODEL = {"provider": "polytropos-spike", "default": "auto"}


def wait_for_memory() -> None:
    while subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                         capture_output=True, text=True).stdout.strip() == "4":
        print("memory pressure level 4, waiting a minute", flush=True)
        time.sleep(60)


def answer_yes(cmd: list[str], env: dict, cwd: Path, timeout_s: int = 600) -> tuple[int, str]:
    """Run *cmd* on a pseudo terminal and type ``y`` whenever it waits at a prompt: output that
    stopped for 2 s without a final newline (rich renders ``[y/N]`` as markup, so it is not shown)."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd)
    os.close(slave)
    out, answered, last, start = bytearray(), 0, time.time(), time.time()
    while time.time() - start < timeout_s:
        if select.select([master], [], [], 0.5)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            out += chunk
            last = time.time()
        elif proc.poll() is not None:
            break
        elif time.time() - last > 2 and len(out) > answered and not out.endswith(b"\n"):
            os.write(master, b"y\n")
            answered = len(out)
            out += b"\n<<typed y>>\n"
    if proc.poll() is None:
        proc.kill()
    os.close(master)
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", out.decode(errors="replace"))
    return proc.wait(), text


def install_pack(root: Path, home: Path, upstream: str) -> None:
    repo = root / "pack" / "spike-repo.git"
    shutil.rmtree(repo.parent, ignore_errors=True)
    for src in (PROVIDER, PROBE):
        shutil.copytree(src, repo / "spikes" / src.name, ignore=shutil.ignore_patterns("__pycache__"))
    git = ["git", "-C", str(repo), "-c", "user.name=spike", "-c", "user.email=spike@localhost",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "spike"], check=True)
    sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    pack = root / "pack" / "hermes-pack.yaml"
    pack.write_text(yaml.safe_dump({
        "name": "polytropos-spike-pack", "description": "Spike pack with two subfolder plugins",
        "plugins": [{"repo": repo.as_uri(), "ref": sha, "subdir": f"spikes/{src.name}"} for src in (PROVIDER, PROBE)],
        "config": {"polytropos-spike": {"upstream": upstream},
                   "hook-probe": {"probe": "seeded-top-level", "settings": {"probe": "seeded-settings"}}},
    }, sort_keys=False))
    code, text = answer_yes([str(hermes_sandbox.HERMES_BIN), "plugins", "pack", "install", str(pack)],
                            hermes_sandbox.sandbox_env(home), root)
    (root / "pack-install.txt").write_text(f"exit {code}\n{text}")
    print(f"pack install exit {code}", flush=True)


def main(root: Path, placement: str) -> None:
    home = root / "hermes"
    hermes_sandbox.stop_gateway(home)  # a daemon left by the previous run would serve this one
    shutil.rmtree(home / "plugins", ignore_errors=True)
    (root / "work" / "out.txt").unlink(missing_ok=True)
    (home / "spike.jsonl").unlink(missing_ok=True)
    with FakeOpenAI() as fake:
        fake.script("main", {"tool_calls": [{"name": "write_file",
                                             "arguments": json.dumps({"path": "out.txt", "content": "hi"})}]},
                    {"content": "Wrote out.txt."})
        if placement == "pack":
            home = hermes_sandbox.make_home(root, {"model": MODEL}, [])
            install_pack(root, home, fake.base_url)
        else:
            entries = {"polytropos-spike": {"upstream": fake.base_url},
                       "hook-probe": {"probe": "entry-top-level", "settings": {"probe": "entry-settings"}}}
            home = hermes_sandbox.make_home(root, {"model": MODEL, "plugins": {"enabled": ["hook-probe"],
                                                                              "entries": entries}}, [PROBE])
            sub = "model-providers/polytropos-spike" if placement == "nested" else "polytropos-spike"
            shutil.copytree(PROVIDER, home / "plugins" / sub, ignore=shutil.ignore_patterns("__pycache__"))
        (home / ".env").unlink(missing_ok=True)
        if placement != "nokey":
            (home / ".env").write_text(KEYS)
        wait_for_memory()
        result = hermes_sandbox.run_oneshot(home, "Create out.txt with the text hi.",
                                            provider="polytropos-spike", model="auto")
        requests = [{"path": r["_path"], "model": r.get("model"), "auth": r["_auth"], "stream": r.get("stream"),
                     "stream_options": r.get("stream_options"), "roles": [m["role"] for m in r.get("messages", [])],
                     "tools": len(r.get("tools") or [])} for r in fake.requests]
    out_file = root / "work" / "out.txt"
    (root / f"run-{placement}.json").write_text(json.dumps({
        "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "requests": requests,
        "out_txt": out_file.read_text() if out_file.exists() else None}, indent=1))
    if (home / "spike.jsonl").exists():
        shutil.move(home / "spike.jsonl", root / f"spike-{placement}.jsonl")
    print(f"{placement}: exit {result.returncode}, {len(requests)} fake requests, "
          f"out.txt={out_file.read_text() if out_file.exists() else None!r}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2])
