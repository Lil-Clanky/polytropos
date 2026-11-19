import http.client
import json
import time
from pathlib import Path

import pytest

from polytropos_shared import canary

FIXTURE = Path(__file__).parent / "data" / "matrix.json"


def fixture_matrix():
    return canary.parse_matrix(json.loads(FIXTURE.read_text()))


def test_verdicts_from_fixture():
    matrix = fixture_matrix()
    assert matrix.verdict("0.40.16", "qwen3.5:0.8b") == "fail"
    assert matrix.verdict("0.40.16", "qwen3.5:0.8b", ["A1", "A2"]) == "pass"
    assert matrix.verdict("0.40.16", "llama3.2:3b") == "pass"
    assert matrix.verdict("0.40.16", "nope:1b") == "unknown"
    assert matrix.verdict("0.1.0", "llama3.2:3b") == "unknown"


def test_passing_versions_sorted():
    cells = [canary.Cell(v, "m", c, "pass") for v in ("0.9.0", "0.40.16", "0.40.9") for c in canary.TOOL_CHECKS]
    cells += [canary.Cell("0.41.0", "m", "A6", "fail"), canary.Cell("0.41.0", "m", "A1", "pass")]
    assert canary.Matrix(cells).passing_versions("m") == ["0.40.16", "0.40.9", "0.9.0"]
    assert fixture_matrix().passing_versions("qwen3.5:0.8b") == []
    assert fixture_matrix().passing_versions("llama3.2:3b") == ["0.40.16", "0.40.15"]


def test_schema_mismatch_raises():
    with pytest.raises(ValueError):
        canary.parse_matrix({"schema": 2, "cells": []})


def test_cache_fresh_skips_fetch(tmp_path):
    cache = tmp_path / "m.json"
    cache.write_text(FIXTURE.read_text())
    now = cache.stat().st_mtime + 100

    def boom(url):
        raise AssertionError("fetch called")

    matrix = canary.load_matrix(cache_path=cache, fetch=boom, now=now)
    assert matrix.verdict("0.40.16", "llama3.2:3b") == "pass"


def test_stale_cache_refetched_and_rewritten(tmp_path):
    cache = tmp_path / "sub" / "m.json"
    cache.parent.mkdir()
    cache.write_text('{"schema": 1, "cells": []}')
    now = cache.stat().st_mtime + 86401
    urls = []

    def fetch(url):
        urls.append(url)
        return FIXTURE.read_bytes()

    matrix = canary.load_matrix(cache_path=cache, fetch=fetch, now=now)
    assert urls == [canary.MATRIX_URL]
    assert matrix.verdict("0.40.16", "qwen3.5:0.8b") == "fail"
    assert json.loads(cache.read_text())["cells"]


def test_stale_cache_used_when_fetch_fails(tmp_path):
    cache = tmp_path / "m.json"
    cache.write_text(FIXTURE.read_text())

    def fail(url):
        raise OSError("offline")

    matrix = canary.load_matrix(cache_path=cache, fetch=fail, now=cache.stat().st_mtime + 10**6)
    assert matrix.verdict("0.40.16", "llama3.2:3b") == "pass"


def test_no_cache_no_network_is_empty(tmp_path):
    def fail(url):
        raise OSError("offline")

    matrix = canary.load_matrix(cache_path=tmp_path / "none.json", fetch=fail, now=time.time())
    assert matrix.verdict("0.40.16", "llama3.2:3b") == "unknown"


def test_truncated_download_falls_back(tmp_path):
    def truncated(url):
        raise http.client.IncompleteRead(b"")

    cache = tmp_path / "m.json"
    cache.write_text(FIXTURE.read_text())
    stale = cache.stat().st_mtime + 10**6
    assert canary.load_matrix(cache_path=cache, fetch=truncated, now=stale).verdict("0.40.16", "llama3.2:3b") == "pass"
    empty = canary.load_matrix(cache_path=tmp_path / "none.json", fetch=truncated, now=time.time())
    assert empty.verdict("0.40.16", "llama3.2:3b") == "unknown"


def test_bad_fetch_does_not_overwrite_cache(tmp_path):
    cache = tmp_path / "m.json"
    cache.write_text(FIXTURE.read_text())
    matrix = canary.load_matrix(cache_path=cache, fetch=lambda u: b'{"schema": 9}', now=cache.stat().st_mtime + 10**6)
    assert matrix.verdict("0.40.16", "llama3.2:3b") == "pass"
    assert json.loads(cache.read_text())["schema"] == 1


def test_default_cache_path_in_cache_dir(tmp_home):
    canary.load_matrix(fetch=lambda u: FIXTURE.read_bytes(), now=time.time())
    assert (tmp_home / ".cache" / "polytropos" / "canary-matrix.json").exists()


def test_local_results_roundtrip(tmp_home):
    assert canary.load_local_results() == {}
    canary.save_local_results({"groq/llama-3.3-70b": "pass", "x/y": "fail"})
    assert canary.local_results_path() == tmp_home / ".local" / "share" / "polytropos" / "canary" / "openai-results.json"
    assert canary.load_local_results() == {"groq/llama-3.3-70b": "pass", "x/y": "fail"}


@pytest.mark.parametrize("garbage", ["not json {", "[]", "null"])
def test_corrupt_cache_does_not_crash(tmp_path, garbage):
    cache = tmp_path / "m.json"
    cache.write_text(garbage)
    assert canary.load_matrix(cache_path=cache, fetch=lambda u: FIXTURE.read_bytes(), now=time.time() + 10).verdict("0.40.16", "llama3.2:3b") == "pass"
    cache.write_text(garbage)

    def fail(url):
        raise OSError

    assert canary.load_matrix(cache_path=cache, fetch=fail).verdict("0.40.16", "llama3.2:3b") == "unknown"
    assert canary.load_matrix(cache_path=tmp_path / "n.json", fetch=lambda u: garbage.encode()).verdict("a", "b") == "unknown"
