"""Turns through the real Hermes in the isolated root that build-root makes, against loopback fakes.

Opt in with POLYTROPOS_E2E=1 after building the root once:
    python scripts/hermes_sandbox.py build-root .sandbox/e2e
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_sandbox
from fake_openai import FakeOpenAI
from polytropos_router.config import load_config, parse_config
from polytropos_shared import guard

ROOT = Path(__file__).resolve().parents[2] / ".sandbox" / "e2e"
RUNTIME = "com.nousresearch.hermes.runtime."  # one-off gateway jobs; Hermes removes finished ones itself
pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(os.environ.get("POLYTROPOS_E2E") != "1", reason="set POLYTROPOS_E2E=1 to run")]


@pytest.fixture(scope="module", autouse=True)
def launchd_before() -> set[str]:
    if not (ROOT / "hermes").is_dir():
        pytest.skip("the e2e root is missing; build it with: python scripts/hermes_sandbox.py build-root .sandbox/e2e")
    return hermes_sandbox.launch_snapshot()


def _router(upstreams: dict[str, FakeOpenAI], routes: dict[str, list[str]], classifier: str | None = None) -> dict:
    return {"upstreams": {name: {"base_url": fake.base_url} for name, fake in upstreams.items()},
            "routes": routes, "classifier": classifier}


def _home(router: dict) -> Path:
    """The root's home with fresh plugin copies and *router* as the router's entry."""
    # Memory pressure and the root's free disk only: the swap limit is for local model runs, and these start no model.
    verdict = guard.judge(guard.read_signals(disk_path=ROOT), guard.Limits(max_swap_bytes=sys.maxsize))
    if not verdict.allowed:
        pytest.skip(f"no Hermes run while {verdict.reason}")
    home = hermes_sandbox.router_home(ROOT, router)
    # The router falls back to its defaults, FreeLLMAPI on 127.0.0.1:3001, when its entry does not parse.
    assert load_config(home) == (parse_config(router), [])
    return home


def _turn(router: dict, prompt: str) -> subprocess.CompletedProcess:
    return hermes_sandbox.run_oneshot(_home(router), prompt, provider="polytropos-router", model="auto")


def test_routed_turn_reaches_complex_upstream():
    with FakeOpenAI() as small, FakeOpenAI() as big:
        small.script("clf", {"content": "complex"})
        big.script("big", {"content": "The complex tier answered."})
        result = _turn(_router({"small": small, "big": big}, {"simple": ["small/chat"], "complex": ["big/big"]},
                               classifier="small/clf"),
                       "Plan the refactor of a large module in three steps.")

    assert result.returncode == 0, result.stderr
    assert "The complex tier answered." in result.stdout
    assert [r["model"] for r in small.requests] == ["clf"]
    assert [(r["model"], r["stream"]) for r in big.requests] == [("big", True)]


def test_broken_tool_call_retried_on_next_model():
    out = ROOT / "work" / "out.txt"
    out.unlink(missing_ok=True)
    with FakeOpenAI() as first, FakeOpenAI() as second:
        first.script("first", {"tool_calls": [{"name": "write_file", "arguments": "{bad"}]})
        second.script("second", {"tool_calls": [{"name": "write_file",
                                                 "arguments": json.dumps({"path": "out.txt", "content": "hi"})}]},
                      {"content": "Wrote out.txt."})
        result = _turn(_router({"first": first, "second": second}, {"complex": ["first/first", "second/second"]}),
                       "Create out.txt with the text hi.")

    assert result.returncode == 0, result.stderr
    assert out.read_text() == "hi"
    assert [r["model"] for r in first.requests] == ["first"]
    assert [r["model"] for r in second.requests] == ["second", "second"]  # the retry, then the turn after the tool
    assert second.requests[0]["messages"] == first.requests[0]["messages"]


def test_doctor_runs_in_sandbox():
    with FakeOpenAI() as fake:
        home = _home(_router({"fake": fake}, {"complex": ["fake/model"]}))
        matrix = ROOT / "home" / ".cache" / "polytropos" / "canary-matrix.json"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(json.dumps({"schema": 1, "cells": []}))  # fresh, so the doctor reads it and downloads none
        env = {**hermes_sandbox.sandbox_env(home),
               # The doctor probes FreeLLMAPI on the fixed 127.0.0.1:3001. A proxy on a closed port refuses that
               # probe, and no_proxy lets the probe of the fake upstream through.
               "http_proxy": f"http://{hermes_sandbox.CLOSED}", "no_proxy": fake.base_url.split("/")[2]}
        result = subprocess.run([str(hermes_sandbox.HERMES_BIN), "polytropos", "doctor"], cwd=ROOT / "work", env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)

    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "Polytropos doctor" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert re.search(r"^\s*ok\s+router config", result.stdout, re.M), result.stdout  # it imported the router config
    assert "FreeLLMAPI is not running" in result.stdout  # the probe of port 3001 ended at the closed proxy


def _running_runtime_jobs() -> set[str]:
    listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=True).stdout
    return {fields[2] for fields in (line.split("\t") for line in listing.splitlines()[1:])
            if len(fields) == 3 and fields[0] != "-" and fields[2].startswith(RUNTIME)}


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd runs only on macOS")
def test_no_launchd_changes(launchd_before):  # last, so it covers the runs of the tests above
    with FakeOpenAI() as fake:  # and a run of its own, so it never passes without one
        assert _turn(_router({"fake": fake}, {"complex": ["fake/model"]}), "Say hi.").returncode == 0

    def stable(snapshot: set[str]) -> set[str]:
        return {label for label in snapshot if not label.startswith((RUNTIME, "application."))}

    assert stable(hermes_sandbox.launch_snapshot()) == stable(launchd_before)
    assert not _running_runtime_jobs() - launchd_before
