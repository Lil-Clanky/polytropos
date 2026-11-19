# Polytropos v0.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Polytropos v0.1.0, a Hermes Agent plugin pack with a routing model provider, a general plugin (doctor, FreeLLMAPI manager, memory guard hook, sleep hold), two stack-canary additions, and release tooling.

**Architecture:** One repository holds two Hermes plugins. `polytropos-router` is a `kind: model-provider` plugin whose `create_client` returns an OpenAI-client facade that classifies each turn and forwards it to OpenAI-compatible upstreams. `polytropos` is a general plugin that registers `hermes polytropos`, `/polytropos` and three hooks. Code both plugins need lives in `shared/polytropos_shared/` and is copied into each plugin's `_shared/` package by `scripts/sync_shared.py`.

**Tech Stack:** Python 3.11 standard library, `openai` (shipped with Hermes), the Hermes public plugin API, pytest, GitHub Actions, launchd and systemd user units.

**Spec:** `docs/superpowers/specs/2026-11-19-polytropos-design.md`

## Global Constraints

- Python `>=3.11` (Hermes v0.23.2 runs 3.11.15). Runtime imports: the standard library, `openai`, and Hermes modules listed in `~/.hermes/hermes-agent/hermes_cli/data/plugin_api.json` (for example `providers`, `providers.base`, `hermes_yaml`, `utils:atomic_json_write`). pytest is the only dev dependency.
- Reference Hermes: v0.23.2, commit `ae2a81ccc00`, checked out at `~/.hermes/hermes-agent`. Treat it as read only. The closed router PR is on its local branch `feat/model-router-virtual-provider`.
- Hermes refuses models whose reported context is below `64_000` tokens.
- FreeLLMAPI: repository `https://github.com/tashfeenahmed/freellmapi.git`, port `3001`, `HOST=127.0.0.1`, `NODE_ENV=production`, Node `>=20.18.0 <25.0.0`, started as `node current/server/dist/index.js`. Database `server/data/freeapi.db` and key `server/data/.encryption-key`; its `.env` moves with `FREEAPI_ENV_PATH`.
- Paths: data `${XDG_DATA_HOME:-~/.local/share}/polytropos`, state `${XDG_STATE_HOME:-~/.local/state}/polytropos`, cache `${XDG_CACHE_HOME:-~/.cache}/polytropos`, Hermes home `$HERMES_HOME` or `~/.hermes`.
- Service names: launchd label `io.github.lil-clanky.polytropos.freellmapi`, systemd unit `polytropos-freellmapi.service`.
- Guard defaults: refuse at macOS memory pressure warning or worse (`kern.memorystatus_vm_pressure_level`: 1 normal, 2 warning, 4 critical), swap above `4 * 1024**3` bytes, free disk below `5 * 1024**3` bytes. Linux PSI starting threshold `some avg10 >= 10.0`, documented as an untuned starting value.
- Router: classifier `max_tokens` 16, classifier timeout 8 s, `default_route` `complex`, traces off by default and written with mode `0o600`, cloud window `128_000` when a cloud upstream has none configured.
- Awake: maximum hold `7200` s. Backups: keep the newest `5`. Canary matrix cache: refresh at most every `86400` s. Canary checks on cloud models: 1 attempt per check.
- Pack config seeds may not contain keys matching `(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key|credential|auth)`.
- User-facing text (README, docs, doctor output, plugin descriptions): plain sentences, no em dashes, no slogans, no "X, not Y" contrasts, no rhetorical questions, no invented data.
- Commits use the repository's local git identity (Lil-Clanky, noreply address), which is already set, and carry no attribution trailers. Packages that run in parallel each work in their own git worktree under `.worktrees/<package>` on branch `pkg/<package>`; the controller rebases and merges them into `build/v0.1.0`.
- Tests do not touch the real `~/.hermes`, `~/Library/LaunchAgents`, systemd user units, anything on ports 3001 or 11434, or local models. Network traffic in tests goes only to loopback fake servers that the test starts.

## Review Focus

1. Multimodal user content: Hermes sends a user message's `content` as a list of parts. Turn keys, bridge lookups, token estimates and the classifier must use the joined text parts and must not crash. (Tests in Tasks 3, 6, 7.)
2. A stream that fails after text was emitted: the router must pass the error on without a retry, because a retry would repeat text the user already saw. (Test in Task 8.)
3. Port 3001 is taken by a program that is not FreeLLMAPI, or FreeLLMAPI is down: the doctor says which, and `install` refuses to start a second service on that port. (Tests in Tasks 13, 15, 17.)
4. A broken router config (unknown upstream in a route, a slot without `/`, a negative window): Hermes must still start. `load_config` falls back to defaults and returns the errors, and the doctor shows them. (Tests in Tasks 6, 15.)
5. Concurrent gateway sessions on several threads: the decision cache, the bridge and the sleep hold counter stay consistent. (Tests in Tasks 3, 7, 16.)

---

## File structure

```
README.md, LICENSE (MIT), .gitignore, pyproject.toml (pytest config only)
hermes-pack.yaml, versions.lock
shared/polytropos_shared/{__init__,paths,bridge,guard,canary}.py
plugins/polytropos_router/{plugin.yaml,__init__,config,tokens,decisions,classifier,toolcheck,streams,client,trace}.py, _shared/ (generated)
plugins/polytropos/{plugin.yaml,__init__,state,services,freellm,programs,doctor,hooks,awake,cli,slash}.py, _shared/ (generated)
plugins/whatsapp_human_pace/ (adapted copy)
scripts/{sync_shared,hermes_sandbox,release,check_updates}.py
tests/{conftest.py,fake_openai.py,test_*.py}, tests/e2e/test_hermes_e2e.py
.github/workflows/{test,bump}.yml
docs/reference.md, docs/spikes/2026-11-19-provider-plugin.md
```

Plugin folders use underscores so tests can import them as packages. Each `plugin.yaml` carries the hyphenated plugin name. The generated `_shared/` copies are committed, because a pack installs a plugin folder straight from git.

## Execution order

Tasks are handed out in packages; one implementer works through a package task by task, and a reviewer checks each package before the next wave starts.

1. Tasks 1 and 3.
2. In parallel: Task 2 (spike); Tasks 4 and 5; Tasks 6, 7 and 8; Tasks 11, 12 and 13; Task 14; Tasks 18 and 19; Task 21. The controller reads the spike findings and corrects Tasks 9, 10, 16, 17 and 22 before wave 3.
3. In parallel: Tasks 9 and 10; Tasks 15 and 16; Task 22.
4. Tasks 17 and 20.
5. Task 23, then Task 24, then a review of the whole branch.

---

### Task 1: Repository scaffold and test harness

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `.gitignore`, `shared/polytropos_shared/__init__.py`, `scripts/sync_shared.py`, `tests/conftest.py`, `tests/fake_openai.py`, `tests/test_harness.py`, `.github/workflows/test.yml`

**Interfaces:**
- Produces `tests/fake_openai.py`:
  - `FakeOpenAI()`, a context manager running a loopback HTTP server. Attributes `base_url` (`http://127.0.0.1:<port>/v1`) and `requests: list[dict]` (parsed JSON bodies plus `"_path"` and `"_auth"` keys).
  - `script(model: str, *replies: dict) -> None` queues replies per model. A reply is `{"content": str}`, `{"tool_calls": [{"name": str, "arguments": str}]}`, or `{"status": int}` for an HTTP error. With `stream=True` the same reply goes out as SSE chunks (text in 3 chunks, tool calls as deltas). `{"content": str, "fail_after": n}` drops the connection after `n` chunks. Any reply may add `"delay": seconds`, which the server waits before answering (and between stream chunks).
  - Serves `GET /v1/models` (`{"data": [{"id": m}]}` for scripted models) and `POST /v1/chat/completions`. An empty queue answers `{"content": "ok"}`.
  - Its server class overrides `server_bind` to skip the reverse DNS lookup, as stack-canary's `tests/fake_ollama.py` does.
- Produces `scripts/sync_shared.py`: `sync(root: Path) -> list[Path]` copies `shared/polytropos_shared/*.py` into `plugins/polytropos/_shared/` and `plugins/polytropos_router/_shared/` and returns the written paths. It writes a file only when its content differs, through a temp file and `os.replace`, because several test runs may sync at the same time. `check(root: Path) -> list[str]` returns the differing files. `python scripts/sync_shared.py --check` exits 1 when anything differs.
- Produces `tests/conftest.py`: puts `shared/` and `plugins/` on `sys.path`, runs `sync()` once per session, and offers the fixtures `fake_openai` (a started `FakeOpenAI`) and `tmp_home` (a temp folder set as `HOME`, `HERMES_HOME` (`<tmp>/.hermes`), `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` through monkeypatch).

- [ ] **Step 1: Write the failing tests in `tests/test_harness.py`**

```python
def test_fake_openai_non_stream_and_models(fake_openai):
    # script("m1", {"content": "hi"}); openai.OpenAI(base_url=fake_openai.base_url, api_key="x")
    # chat.completions.create(model="m1", messages=[...]).choices[0].message.content == "hi"
    # models.list() ids contain "m1"; fake_openai.requests[-1]["model"] == "m1"
def test_fake_openai_stream_tool_call(fake_openai):
    # script("m1", {"tool_calls": [{"name": "write_file", "arguments": '{"path": "a"}'}]})
    # joined tool_call argument deltas of the stream == '{"path": "a"}'
def test_fake_openai_http_error(fake_openai):
    # script("m1", {"status": 429}) -> openai.RateLimitError
def test_sync_shared_check(tmp_path):
    # copy shared/ and empty plugin folders to tmp_path; sync(); check() == []
    # edit one copy; check() names that file
def test_tmp_home_isolates(tmp_home):
    # Path.home() == tmp_home and os.environ["HERMES_HOME"] == str(tmp_home / ".hermes")
```

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_harness.py -q` (with a Python that has `openai` and `pytest`, for example the Hermes venv `~/.hermes/hermes-agent/venv/bin/python` or a local `.venv`).
Expected: errors for missing modules.

- [ ] **Step 3: Implement the files**

`pyproject.toml` holds only `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and the marker `e2e`. Create the local test environment with `uv venv --python 3.11 .venv` and `uv pip install --python .venv/bin/python openai pytest pyyaml`; every later task runs tests with `.venv/bin/python -m pytest`. `test.yml`: matrix `ubuntu-latest`, `macos-latest` and Python `3.11`, `3.13`; steps `pip install openai pytest pyyaml`, `python scripts/sync_shared.py --check`, `python -m pytest -q -m "not e2e"`. `.gitignore`: `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.venv/`. `LICENSE`: MIT, copyright Lil-Clanky.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit** "Add the repository scaffold and test harness"

### Task 2: Spike, router provider plugin in an isolated Hermes home

**Files:**
- Create: `spikes/provider_min/plugin.yaml`, `spikes/provider_min/__init__.py`, `spikes/hook_probe/plugin.yaml`, `spikes/hook_probe/__init__.py`, `scripts/hermes_sandbox.py`, `docs/spikes/2026-11-19-provider-plugin.md`

**Interfaces:**
- Consumes: `FakeOpenAI` (Task 1).
- Produces `scripts/hermes_sandbox.py` (Task 24 reuses it):
  - `make_home(root: Path, config: dict, plugins: list[Path]) -> Path` writes `config.yaml` and copies the plugin folders into `<home>/plugins/`.
  - `run_oneshot(home: Path, prompt: str, *, provider: str, model: str, timeout_s: int = 180) -> subprocess.CompletedProcess` runs `~/.hermes/hermes-agent/venv/bin/hermes` in one-shot mode (check the exact flags with `--help`) with `HERMES_HOME=home` and `HOME=<root>/home`.
  - `launch_snapshot() -> set[str]`: labels from `launchctl list` that contain `hermes` or `polytropos`, plus the file names in `~/Library/LaunchAgents` (an empty set on Linux).

- [ ] **Step 1:** Write a minimal provider plugin `polytropos-spike` (`kind: model-provider`): a `ProviderProfile` subclass whose `create_client(**kw)` returns a facade shaped like `agent.acp_client.ACPClient` (attributes `api_key`, `base_url`, `chat.completions.create`, `is_closed`, method `close()`, class attributes `HERMES_SKIP_TRANSPORT_WRAP = True` and `HERMES_SKIP_ASYNC_WRAP = True`). It forwards every call to an `openai.OpenAI` client for the fake server and appends a JSON line per call with the roles of the messages, `stream`, the tool names, the thread id and the `client_kwargs` keys. `get_model_context_length` returns `128_000`. Write `hook_probe`, a general plugin that logs the thread id and every kwarg name of `pre_llm_call` and `pre_api_request`.
- [ ] **Step 2:** Write `scripts/hermes_sandbox.py`. Script the fake server with a `write_file` tool call (`{"path": "out.txt", "content": "hi"}`) followed by a text reply.
- [ ] **Step 3:** Run one turn with the spike provider for each placement: `<home>/plugins/polytropos-spike/`, `<home>/plugins/model-providers/polytropos-spike/`, and `hermes plugins pack install` of a local pack file that points at a local git repository with `subdir:` (feed `y` on stdin if it prompts, and record whether that works). Compare `launch_snapshot()` before and after; stop and report if it changed. Also try `hermes plugins install DietrichGebert/ponytail` at the commit from `versions.lock` (or the latest commit when the lock is not written yet) in the sandbox, and record whether the install check fails.
- [ ] **Step 4:** Write the findings to `docs/spikes/2026-11-19-provider-plugin.md`, each with its evidence:
  1. Which placement loads the provider, and whether it loads as a package (relative imports work).
  2. Whether `base_url="polytropos://router"` is accepted, which `client_kwargs` keys arrive, and whether `api_key` carries the value of the profile's `env_vars` entry.
  3. Whether Hermes calls `create` with `stream=True`, every kwarg key it passes to `create`, and which attributes of the returned object or stream it touches (log `AttributeError`s).
  4. The order of message roles across the tool loop of one turn, to confirm the turn rule (a new decision when the last message is from `user`).
  5. Whether `pre_llm_call` and the API call run on the same thread, every kwarg `pre_llm_call` receives, and where context returned by `pre_llm_call` ends up in the request (appended to the user message, prepended, or a separate message). The bridge lookup in Task 3 assumes the stored user text is a prefix of the request's last user message.
  6. Which `plugin.yaml` fields a pack install of a subfolder needs (`manifest_version`, `api_version` and so on), and how the installed folder is named.
  7. Whether ponytail installs at its pinned commit.
  8. Spec spike 3: whether a pack can install a plugin from a pip package with a `hermes_agent.plugins` entry point (read `hermes_cli/plugin_packs.py`). If it cannot, the `_shared/` copies stay.
  The gateway half of spec spike 1 is skipped: the sandbox has no messaging platform, and the maintainer's gateway stays untouched. Say so in the findings.
- [ ] **Step 5:** When a finding contradicts the Interfaces of Tasks 9, 10, 16, 17 or 22, correct those tasks in this plan. If the provider cannot load or stream at all, stop and switch the router to the proxy fallback from spec risk 1 before Task 9. Commit "Add the provider plugin spike".

### Task 3: Shared paths and bridge

**Files:**
- Create: `shared/polytropos_shared/paths.py`, `shared/polytropos_shared/bridge.py`
- Test: `tests/test_paths_bridge.py`

**Interfaces:**
- Produces `paths.py`: `data_dir() -> Path`, `state_dir() -> Path`, `cache_dir() -> Path`, `hermes_home() -> Path`, each reading the environment when called (defaults in Global Constraints).
- Produces `bridge.py`:
  - `@dataclass(frozen=True) class TurnInfo: session_id: str; platform: str; user_text: str; created: float`
  - `message_text(content: str | list | None) -> str` joins the `text` of `{"type": "text"}` parts with `"\n"` and ignores other parts.
  - `remember_turn(info: TurnInfo) -> None` and `lookup_turn(last_user_text: str, *, now: float | None = None) -> TurnInfo | None`. The lookup returns the newest entry whose `user_text` equals `last_user_text` or is a prefix of it, skipping entries older than 3600 s. At most 512 entries are kept.
  - The store is shared by every copy of this module in the process: keep it on `sys.modules.setdefault("polytropos_bridge_store", types.ModuleType("polytropos_bridge_store"))`, guarded by a `threading.Lock` stored there too.

- [ ] **Step 1: Write the failing tests**

```python
def test_paths_follow_xdg(tmp_home, monkeypatch):
    # XDG_DATA_HOME=/x -> data_dir() == Path("/x/polytropos"); unset -> Path.home() / ".local/share/polytropos"
def test_hermes_home_env_and_default(monkeypatch): ...
def test_message_text_handles_parts():
    assert message_text([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}, {"type": "text", "text": "b"}]) == "a\nb"
    assert message_text(None) == ""
def test_lookup_prefix_match():
    # remember TurnInfo("s1", "whatsapp", "hello", t); lookup_turn("hello\n\n[extra context]").platform == "whatsapp"
def test_lookup_expires():
    # created at t; lookup_turn("hello", now=t + 3601) is None
def test_store_shared_between_module_copies():
    # load bridge.py twice with importlib under two module names; remember via copy A, lookup via copy B
def test_concurrent_remember():
    # 8 threads x 200 remember_turn calls; no exception; stored entries <= 512
```

- [ ] **Step 2:** `python -m pytest tests/test_paths_bridge.py -q` fails (module missing).
- [ ] **Step 3:** Implement `paths.py` and `bridge.py` with the signatures above.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add shared paths and the plugin bridge"

### Task 4: Shared memory guard

**Files:**
- Create: `shared/polytropos_shared/guard.py`
- Test: `tests/test_guard.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Signals: pressure: str; swap_bytes: int | None; free_disk_bytes: int | None` with `pressure` in `"normal" | "warning" | "critical" | "unknown"`.
  - `@dataclass(frozen=True) class Limits: max_swap_bytes: int = 4 * 1024**3; min_free_disk_bytes: int = 5 * 1024**3; psi_some_avg10: float = 10.0`
  - `@dataclass(frozen=True) class Verdict: allowed: bool; reason: str = ""`
  - `read_signals(*, platform: str = sys.platform, run=subprocess.run, read_text=lambda p: Path(p).read_text(), disk_path: Path | None = None, limits: Limits = Limits()) -> Signals`. macOS: `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level` and `/usr/sbin/sysctl -n vm.swapusage` (parse `used = 1536.25M`). Linux: `/proc/pressure/memory`, where `some avg10 >= psi_some_avg10` means `"warning"` and `full avg10 >= psi_some_avg10` means `"critical"`; swap is `SwapTotal - SwapFree` from `/proc/meminfo` (kB). Disk: `shutil.disk_usage(disk_path or <~/.ollama/models if it exists, else home>).free`. A failed read gives `"unknown"` or `None`.
  - `judge(signals: Signals, limits: Limits = Limits()) -> Verdict` refuses on `warning` or `critical`, on swap above the limit and on disk below the limit. Unknown values are allowed. Reasons name the signal ("memory pressure is warning", "swap use is 5.0 GB", "free disk is 4.0 GB").
  - `ollama_root(base_url: str) -> str` strips a trailing `/v1`. `ollama_loaded(base_url: str, *, timeout: float = 2.0) -> list[dict]` reads `GET {root}/api/ps` (`models`). `ollama_unload(base_url: str, model: str, *, timeout: float = 5.0) -> bool` posts `{"model": model, "keep_alive": 0}` to `{root}/api/generate`.
  - `kv_cache_bytes(model_info: dict, context: int) -> int | None` from the `/api/show` `model_info` keys ending in `.block_count`, `.attention.head_count_kv`, `.embedding_length` and `.attention.head_count`: `2 * layers * kv_heads * (embedding // heads) * context * 2`.
  - `fits(model_bytes: int, kv_bytes: int, total_ram_bytes: int, loaded_bytes: int, reserve_bytes: int = 4 * 1024**3) -> bool` is `model + kv + loaded + reserve <= total`, with the comment `# shortcut: rough estimate, the watchdog is the safety net; replace when coffee-vram or measurements say so`.
  - `total_ram_bytes(*, platform: str = sys.platform, run=subprocess.run, read_text=lambda p: Path(p).read_text()) -> int | None`: `/usr/sbin/sysctl -n hw.memsize` on macOS, `MemTotal` (kB) from `/proc/meminfo` on Linux, `None` when unreadable.
  - `ollama_fit(base_url: str, model: str, context: int, *, total_ram: int | None, timeout: float = 2.0) -> bool | None`: `True` when `/api/ps` lists the model; otherwise the model's `size` from `GET {root}/api/tags`, `kv_cache_bytes` from `POST {root}/api/show` (`{"model": model}`) for `context`, and the summed `size` of the models in `/api/ps`, passed to `fits`. `None` when `total_ram` is `None` or any lookup fails.
  - `class Watchdog(limits: Limits = Limits(), *, interval_s: float = 2.0, read: Callable[[], Signals] = read_signals, on_trip: Callable[[str], None])`, a context manager that polls on a daemon thread and calls `on_trip(reason)` once when pressure is `critical` or swap passes the limit. Attribute `tripped: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_macos_pressure_levels():      # fake run returning "1", "2", "4" -> normal, warning, critical
def test_macos_swap_parse():           # "total = 2048.00M  used = 1536.25M  free = 511.75M  (encrypted)" -> int(1536.25 * 1024**2)
def test_linux_psi_and_swap():         # some avg10=12.00 -> warning; full avg10=11.00 -> critical; SwapTotal 8000000 kB, SwapFree 7000000 kB -> 1000000 * 1024
def test_unreadable_is_unknown():      # run raises OSError -> pressure "unknown", swap None
def test_judge_rules():                # warning refused ("memory pressure" in reason); 5 GB swap refused; 4 GB disk refused; unknown allowed
def test_kv_cache_llama32_3b():        # block_count 28, head_count_kv 8, embedding_length 3072, head_count 24, context 8192 -> 2*28*8*128*8192*2
def test_fits():                       # fits(2*10**9, 10**9, 16*1024**3, 0) is True; loaded 10*1024**3 -> False
def test_watchdog_trips_once():        # reads normal, critical, critical -> on_trip called once
def test_ollama_helpers_against_fake(): # small http.server with /api/ps and /api/generate; unload body has keep_alive 0 and returns True
def test_total_ram_macos_linux():      # "17179869184" -> 17179869184; "MemTotal:       16000000 kB" -> 16000000 * 1024; failure -> None
def test_ollama_fit_against_fake():    # loaded model -> True; 2 GB model, small kv, 16 GB RAM -> True; 14 GB model -> False; /api/show 500 -> None
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `guard.py`. Then read the catalog entry and repository of the community plugin coffee-vram (`grep -ril coffee-vram ~/.hermes/hermes-agent/plugin-catalog`) and write a short note to `docs/spikes/2026-11-19-coffee-vram.md` on whether it could replace `fits` later (maintenance, license, Apple Silicon memory, network use). Version 0.1.0 ships the built-in check.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the shared memory guard"

### Task 5: Shared canary verdicts

**Files:**
- Create: `shared/polytropos_shared/canary.py`, `tests/data/matrix.json`
- Test: `tests/test_canary_shared.py`

**Interfaces:**
- Consumes: `paths.cache_dir()`, `paths.data_dir()` (Task 3).
- Produces:
  - `MATRIX_URL = "https://raw.githubusercontent.com/Lil-Clanky/stack-canary/matrix/matrix.json"` and `TOOL_CHECKS = ("A1", "A2", "A3", "A4", "A5", "A6")`.
  - The `matrix.json` schema, which Task 18 writes: `{"schema": 1, "generated": "<ISO 8601>", "cells": [{"ollama": "0.40.16", "model": "qwen3.5:0.8b", "check": "A6", "status": "fail", "passed": 0, "attempts": 3}]}` with `status` in `pass | fail | error | n/a`.
  - `@dataclass(frozen=True) class Cell: runtime_version: str; model: str; check: str; status: str`
  - `class Matrix(cells: Iterable[Cell])` with `verdict(runtime_version: str, model: str, checks: Iterable[str] = TOOL_CHECKS) -> str` (`"fail"` when any listed check failed, `"pass"` when all passed, else `"unknown"`) and `passing_versions(model: str) -> list[str]` (versions where every tool check passed, newest first in numeric order).
  - `parse_matrix(data: dict) -> Matrix` raises `ValueError` for another `schema`.
  - `load_matrix(*, cache_path: Path | None = None, fetch: Callable[[str], bytes] | None = None, now: float | None = None, max_age_s: int = 86400) -> Matrix` uses a cache younger than `max_age_s`, otherwise fetches and rewrites the cache, and falls back to the stale cache or an empty `Matrix` when the fetch fails. Default cache path `cache_dir()/canary-matrix.json`.
  - `local_results_path() -> Path` (`data_dir()/canary/openai-results.json`), `load_local_results() -> dict[str, str]` (slot text `"<upstream>/<model>"` -> `"pass" | "fail"`), `save_local_results(results: dict[str, str]) -> None`.

- [ ] **Step 1: Write the failing tests** (the fixture holds `qwen3.5:0.8b` failing A6 and `llama3.2:3b` passing A1 to A6 on `0.40.16` and `0.40.15`)

```python
def test_verdicts_from_fixture():          # qwen3.5:0.8b on 0.40.16 -> "fail"; llama3.2:3b -> "pass"; unknown model -> "unknown"
def test_passing_versions_sorted():        # ["0.40.16", "0.40.15", "0.9.0"] order for a model passing on all three
def test_schema_mismatch_raises():
def test_cache_fresh_skips_fetch():        # fetch must not be called when the cache is younger than 86400 s
def test_stale_cache_used_when_fetch_fails():
def test_no_cache_no_network_is_empty():   # verdict "unknown"
def test_local_results_roundtrip(tmp_home):
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `canary.py`.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add shared canary verdicts"

### Task 6: Router config

**Files:**
- Create: `plugins/polytropos_router/__init__.py` (empty for now), `plugins/polytropos_router/config.py`, `plugins/polytropos_router/tokens.py`
- Test: `tests/test_router_config.py`

**Interfaces:**
- Consumes: `bridge.message_text` (Task 3, as `._shared.bridge`); `hermes_yaml.safe_load` when it imports, else `yaml.safe_load` when PyYAML is present.
- Produces:
  - `@dataclass(frozen=True) class Upstream: name: str; base_url: str; api_key_env: str = ""; local: bool = False; runtime: str = ""; context_window: int = 0`
  - `@dataclass(frozen=True) class Slot: upstream: str; model: str` and `parse_slot(text: str) -> Slot`, which splits on the first `/`.
  - `@dataclass(frozen=True) class RouterConfig: upstreams: dict[str, Upstream]; routes: dict[str, tuple[Slot, ...]]; classifier: Slot | None; classifier_max_tokens: int = 16; classifier_timeout_s: float = 8.0; default_route: str = "complex"; channel_hints: dict[str, str] = field(default_factory=dict); save_traces: bool = False; cloud_window_default: int = 128_000`
  - `DEFAULT_CONFIG`: upstream `freellmapi` at `http://127.0.0.1:3001/v1` with `api_key_env="FREELLMAPI_API_KEY"`, routes `simple` and `complex` both `(Slot("freellmapi", "auto"),)`, classifier `Slot("freellmapi", "auto")`.
  - `class ConfigError(ValueError)` and `parse_config(raw: dict) -> RouterConfig`, which raises `ConfigError` naming the key path, for example `routes.simple[0]`. Upstreams are written as mappings and slots as `"upstream/model"` strings (the spec's YAML example).
  - `load_config(hermes_home: Path) -> tuple[RouterConfig, list[str]]` reads `<hermes_home>/config.yaml` at `plugins.entries["polytropos-router"]`. A missing file or section gives `(DEFAULT_CONFIG, [])`. A section that fails to parse gives `(DEFAULT_CONFIG, [str(error)])`.
  - `window(cfg: RouterConfig, slot: Slot) -> int`: the upstream's `context_window` when set, else `cfg.cloud_window_default` for cloud upstreams, else `0` for local ones.
  - `reported_context(cfg: RouterConfig) -> int` is `max(64_000, min(window of every cloud slot in every route))`, and `cloud_slots_below_floor(cfg: RouterConfig) -> list[Slot]`.
  - `tokens.estimate_tokens(messages: list, tools: list | None = None) -> int` is `len(json.dumps(...)) // 3` over messages (text from `message_text` for list content) and tools, with the comment `# shortcut: character estimate, replace with a tokenizer if routing misjudges sizes`.

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_when_section_missing(tmp_path):   # config.yaml without plugins -> (DEFAULT_CONFIG, [])
def test_parse_spec_example():                     # the spec's YAML example parses; routes["simple"] == (Slot("ollama", "llama3.2:3b"), Slot("freellmapi", "auto"))
def test_slot_with_slash_in_model():               # parse_slot("lmstudio/qwen/qwen3-4b") == Slot("lmstudio", "qwen/qwen3-4b")
def test_unknown_upstream_is_error():              # ConfigError message contains "routes.simple[0]"
def test_slot_without_slash_is_error():
def test_negative_window_is_error():
def test_load_config_falls_back_with_errors(tmp_path):  # broken section -> DEFAULT_CONFIG and one error string
def test_reported_context_floor():                 # cloud window 32000 -> 64000 and that slot is in cloud_slots_below_floor; windows 200000 and 128000 -> 128000
def test_estimate_tokens_list_content():           # multimodal messages give a positive number
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `config.py` and `tokens.py`.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the router config"

### Task 7: Turn decisions and classifier

**Files:**
- Create: `plugins/polytropos_router/decisions.py`, `plugins/polytropos_router/classifier.py`
- Test: `tests/test_decisions.py`

**Interfaces:**
- Consumes: `Slot`, `RouterConfig` (Task 6); `bridge.message_text`, `bridge.lookup_turn` (Task 3).
- Produces:
  - `@dataclass class Decision: tier: str; reason: str; pinned: Slot | None = None` with `reason` in `"classifier" | "default" | "classifier_error" | "hint:<platform>"`.
  - `last_user(messages: list) -> tuple[int, str] | None` (index and text of the last `user` message) and `is_new_turn(messages: list) -> bool` (the last message has role `user`).
  - `class DecisionCache(max_entries: int = 256)`, a thread-safe LRU: `key_for(messages: list) -> str | None` (sha256 of `f"{index}:{text}"` of the last user message), `get(key: str) -> Decision | None`, `put(key: str, decision: Decision) -> None`.
  - `CLASSIFIER_PROMPT: str`, copied from `agent/router_loop.py` on the branch `feat/model-router-virtual-provider` (`git -C ~/.hermes/hermes-agent show feat/model-router-virtual-provider:agent/router_loop.py`).
  - `classify(create: Callable[..., Any], slot: Slot, user_text: str, *, max_tokens: int, timeout_s: float) -> str | None` calls `create(model=slot.model, messages=[{"role": "system", "content": CLASSIFIER_PROMPT}, {"role": "user", "content": user_text}], max_tokens=max_tokens, temperature=0, timeout=timeout_s)` and returns `"simple"` or `"complex"` when the reply contains exactly one of the two words, else `None`. Exceptions give `None`.
  - `decide(cfg: RouterConfig, messages: list, *, classify_fn: Callable[[str], str | None]) -> Decision` tries, in order: a channel hint (`lookup_turn(text).platform` found in `cfg.channel_hints`), the classifier, and `cfg.default_route` with reason `"classifier_error"` when the classifier gave `None`. With no classifier configured the reason is `"default"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_turn_rule():                       # [system, user] -> new; [system, user, assistant(tool_calls), tool] -> not new
def test_key_stable_across_tool_loop():
def test_key_changes_on_next_user_message():
def test_key_handles_multimodal():          # list content -> key is not None
def test_hint_wins_over_classifier():       # bridge has platform "whatsapp", hints {"whatsapp": "simple"} -> Decision("simple", "hint:whatsapp"); classify_fn not called
def test_classifier_result_used():
def test_classifier_none_uses_default():    # reason "classifier_error", tier == cfg.default_route
def test_classify_parses_reply(fake_openai): # "complex." -> "complex"; "simple or complex" -> None; HTTP 500 -> None
def test_cache_thread_safety():             # 8 threads x 500 put/get with max_entries 256 -> size <= 256, no exception
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `decisions.py` and `classifier.py`.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add turn decisions and the classifier"

### Task 8: Tool-call checks and guarded streams

**Files:**
- Create: `plugins/polytropos_router/toolcheck.py`, `plugins/polytropos_router/streams.py`
- Test: `tests/test_toolcheck_streams.py`

**Interfaces:**
- Produces:
  - `broken_tool_calls(tool_calls: list, tools: list[dict] | None) -> list[str]`, accepting OpenAI SDK objects or dicts. Reasons: `unknown tool <name>`, `arguments of <name> are not a JSON object`, `<name> is missing required <key>` (from `tools[i]["function"]["parameters"]["required"]`). An empty list means the calls are valid.
  - `class GuardedStream(upstream: Iterable, *, tools: list[dict] | None, retry: Callable[[], Iterable] | None, on_close: Callable[[], None] | None = None)`, iterable and usable as a context manager, with `close()` and attribute `retried: bool`. `on_close` runs exactly once, when the stream ends, fails or is closed (Task 9 stops its watchdog there). Text deltas pass through as they arrive. Tool-call deltas are held until the upstream ends. When nothing was yielded yet, the assembled tool calls are broken, and `retry` is set, it yields the chunks of `retry()` instead (wrapped the same way, with no further retry). Otherwise it yields the held chunks. An exception after anything was yielded is re-raised. An exception before anything was yielded calls `retry` when it is set.

- [ ] **Step 1: Write the failing tests** (streams from `fake_openai`)

```python
def test_valid_call_passes():
def test_unknown_tool():
def test_bad_json():
def test_missing_required():
def test_stream_text_passthrough_order():         # chunk order kept, retry never called
def test_stream_broken_tool_only_is_retried():    # first stream write_file "{bad"; retry stream valid -> consumer sees only the retry's call; retried is True
def test_stream_text_then_broken_tool_not_retried():
def test_stream_error_after_text_reraises():      # fail_after=1 -> the error surfaces after the first chunk; retry not called
def test_stream_error_before_output_retries():
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `toolcheck.py` and `streams.py`.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add tool-call checks and guarded streams"

### Task 9: Router client

**Files:**
- Create: `plugins/polytropos_router/client.py`, `plugins/polytropos_router/trace.py`
- Test: `tests/test_router_client.py`

**Interfaces:**
- Consumes: Tasks 3 to 8.
- Produces:
  - `class RouterClient(cfg: RouterConfig, *, api_key: str | None = None, openai_factory: Callable[..., Any] = openai.OpenAI, guard_read: Callable[[], Signals] = guard.read_signals, matrix_loader: Callable[[], Matrix] = canary.load_matrix, local_results: Callable[[], dict[str, str]] = canary.load_local_results, runtime_version: Callable[[Upstream], str | None] = <GET {root}/api/version for Ollama upstreams>, fit_check: Callable[[Upstream, str], bool | None] = <guard.ollama_fit with guard.total_ram_bytes() and the upstream's context_window for Ollama upstreams, None for other runtimes>, trace_path: Path | None = None)` with class attributes `HERMES_SKIP_TRANSPORT_WRAP = True` and `HERMES_SKIP_ASYNC_WRAP = True`, attributes `api_key`, `base_url = "polytropos://router"`, `chat` (with `completions.create`), `is_closed`, and method `close()`.
  - Decisions live in one module-level `DecisionCache` shared by every `RouterClient`, because Hermes may create more than one client per session.
  - Upstream clients are made lazily, one per upstream: `openai_factory(base_url=u.base_url, api_key=key)`. The key is `api_key` when `u.api_key_env == "FREELLMAPI_API_KEY"` and `api_key` is set, else `os.environ.get(u.api_key_env)`, else `"none"`.
  - `trace.write_trace(path: Path, record: dict) -> None` appends one JSON line and creates the file with mode `0o600`.

Routing in `create(**kwargs)`, in this order:
1. Get the turn's `Decision` from the cache, or make one with `decide` (Task 7). The classifier call goes through the classifier slot's upstream client.
2. Candidates: the tier's chain, then the `complex` chain when the tier is `simple`, without duplicates. A pinned slot that is still eligible goes first.
3. A local slot is eligible when `guard.judge(guard_read())` allows it, `estimate_tokens(messages, tools) + (kwargs.get("max_tokens") or 1024) <= window(cfg, slot)`, `fit_check(upstream, model) is not False`, and, for requests with `tools`, the matrix verdict for (runtime version, model) is not `"fail"`. A cloud slot is eligible when the request has no `tools` or `local_results().get(f"{slot.upstream}/{slot.model}") != "fail"`. The guard, window and fit filters are hard. The two canary filters are soft: when they would leave no candidate, the slots they removed come back in chain order, so a failing canary never makes every tool turn fail.
4. Call `upstream.chat.completions.create(**{**kwargs, "model": slot.model})`. On `openai.RateLimitError`, `openai.APITimeoutError`, `openai.APIConnectionError`, or `openai.APIStatusError` with status 500 or higher, go to the next candidate. Without streaming, when `broken_tool_calls` finds problems and another candidate exists, try that one once. With streaming, return `GuardedStream(result, tools=..., retry=<the same call on the next eligible candidate, or None>)`.
5. Local calls run inside `guard.Watchdog`; for a stream the watchdog stays on until the stream's `on_close`. On a trip, close and drop that upstream's client, which aborts the in-flight HTTP request (the call then raises `openai.APIConnectionError`), and unload the model when the upstream's `runtime` is `"ollama"`. Without streaming, or before any chunk was yielded, the error falls through to the next candidate. After text was yielded the error reaches Hermes (Review Focus 2); Hermes retries the request, and the guard check in step 3 then keeps it off the local model.
6. Set `decision.pinned` to the slot used. When `save_traces` is on, write a record with `time`, `tier`, `reason`, `slot`, `retried` and `fell_back_from`. Return the result.
7. When every candidate fails, raise the last exception.

- [ ] **Step 1: Write the failing tests** (two `FakeOpenAI` servers as the local and the cloud upstream; `guard_read`, the matrix and the local results injected)

```python
def test_simple_turn_goes_local():                    # classifier "simple" -> request on the local server with model "llama3.2:3b"
def test_guard_refusal_skips_local():                 # guard_read -> warning -> cloud
def test_request_too_big_for_local_window():          # window 8192, messages of about 40k characters -> cloud
def test_canary_fail_skips_local_for_tool_turns_only():
def test_rate_limit_falls_back():                     # local 429 -> cloud; trace record has fell_back_from
def test_tool_loop_keeps_decision():                  # second request of the turn: no classifier call, pinned slot used
def test_tool_loop_outgrows_local_window_escalates():
def test_broken_tool_call_retried_non_stream():
def test_all_fail_raises_last():
def test_api_key_from_client_kwargs_for_freellmapi(): # RouterClient(api_key="k") -> the cloud server saw "Bearer k"
def test_traces_off_by_default(tmp_path):
def test_trace_file_mode(tmp_path):                   # 0o600
def test_watchdog_trip_aborts_local_and_falls_back(): # guard_read turns critical during a slow local reply -> cloud answers
def test_fit_refusal_skips_local():                   # fit_check -> False -> cloud; fit_check -> None -> local allowed
def test_canary_filter_never_empties_chain():         # tool turn, the only cloud slot has local result "fail" -> still called
```

Also port the cases from the PR's `tests/agent/test_router_loop.py` and `tests/agent/test_router_trace.py` (`git -C ~/.hermes/hermes-agent show feat/model-router-virtual-provider:<path>`) that still apply to the provider client, such as classifier parsing and fallback order.

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement `client.py` and `trace.py`.
- [ ] **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the router client"

### Task 10: Router provider profile and plugin entry

**Files:**
- Create: `plugins/polytropos_router/plugin.yaml`
- Modify: `plugins/polytropos_router/__init__.py`
- Test: `tests/test_router_plugin.py`

**Interfaces:**
- Consumes: `RouterClient` (Task 9), `load_config` and `reported_context` (Task 6), `paths` (Task 3), `providers.register_provider` and `providers.base.ProviderProfile` from Hermes. Task 2's findings fix the placement and manifest fields.
- Produces:
  - `plugin.yaml`: `name: polytropos-router`, `kind: model-provider`, `version: 0.1.0`, `description: Routes each Hermes turn to a local or free cloud model`, `author: Lil-Clanky`, plus any field Task 2 found necessary.
  - `class PolytroposRouterProfile(ProviderProfile)` with `create_client(**client_kwargs) -> RouterClient` (config from `load_config(paths.hermes_home())`, errors logged, `api_key=client_kwargs.get("api_key")`, trace path `paths.state_dir() / "router-traces.jsonl"`), `get_model_context_length(model) -> int` (`reported_context`), and `fetch_models(self, *, api_key=None, base_url=None, timeout=8.0) -> list[str]` returning `["auto"]`.
  - At import: `register_provider(PolytroposRouterProfile(name="polytropos-router", display_name="Polytropos router", description="Routes each turn to a local or free cloud model", base_url="polytropos://router", env_vars=("FREELLMAPI_API_KEY",), fallback_models=("auto",), supports_streaming=True, supports_health_check=False, supports_model_listing=False))`. The import of `providers` is wrapped so tests can import the package without Hermes; a stub `register_provider` then records the profile in a module attribute.

- [ ] **Step 1: Write the failing tests**

```python
def test_profile_fields():                                # name, base_url, env_vars, fallback_models as above
def test_create_client_uses_hermes_home_config(tmp_home, fake_openai):  # config.yaml routes to fake_openai -> request lands there
def test_context_length_floor(tmp_home):                  # no config -> 128000; cloud window 32000 -> 64000
def test_import_without_hermes():
```

- [ ] **Step 2:** The tests fail.
- [ ] **Step 3:** Implement the profile and registration.
- [ ] **Step 4:** The tests pass. Then check the real lookup with the Hermes venv in a temp home: place the plugin as Task 2 found, and resolve `polytropos-router` with the profile lookup function the spike used. Expected: the profile resolves and `get_model_context_length("auto")` returns `128000`.
- [ ] **Step 5:** Commit "Add the router provider plugin"

### Task 11: State file

**Files:**
- Create: `plugins/polytropos/__init__.py` (empty for now), `plugins/polytropos/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `paths.state_dir()` (Task 3).
- Produces: `@dataclass class State: version: int = 1; profiles: dict[str, dict] = field(default_factory=dict); shared: dict[str, dict] = field(default_factory=dict)`; `load_state(path: Path | None = None) -> State`; `save_state(state: State, path: Path | None = None) -> None` (atomic replace, mode `0o600`); `use_shared(state: State, part: str, profile: str, *, version: str, installed_by_us: bool) -> None`; `release_shared(state: State, part: str, profile: str) -> bool` (True when no profile uses the part any more); `record_config_change(state: State, profile: str, key: str, old: Any, new: Any) -> None` (keeps the first `old` value when a key changes twice); `config_changes(state: State, profile: str) -> list[dict]`; `set_mode(state: State, part: str, mode: str) -> None` for `freellmapi` (`managed | external`). Default path `state_dir() / "state.json"`.

- [ ] **Step 1: Write the failing tests:** roundtrip; file mode `0o600`; a shared part used by two profiles (`release_shared` returns False for the first and True for the second); `installed_by_us` keeps the value of the first install; a key changed twice keeps the original `old`; a missing file gives `State()`; a corrupt file gives `State()` and is renamed to `state.json.corrupt`.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the state file"

### Task 12: Service files

**Files:**
- Create: `plugins/polytropos/services.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Produces: `LAUNCHD_LABEL`, `SYSTEMD_UNIT` (Global Constraints); `@dataclass(frozen=True) class ServiceSpec: program: list[str]; env: dict[str, str]; workdir: Path; log_path: Path`; `render_launchd(spec: ServiceSpec) -> bytes` (`plistlib.dumps` with `Label`, `ProgramArguments`, `EnvironmentVariables`, `WorkingDirectory`, `RunAtLoad: True`, `KeepAlive: {"SuccessfulExit": False}`, `StandardOutPath` and `StandardErrorPath` set to `log_path`); `render_systemd(spec: ServiceSpec) -> str` (`[Unit] Description=Polytropos FreeLLMAPI`, `[Service] ExecStart=` with each argument quoted for systemd, quoted `Environment=` lines, `WorkingDirectory=`, `Restart=on-failure`, `[Install] WantedBy=default.target`); `class ServiceManager(*, platform: str = sys.platform, run=subprocess.run, home: Path | None = None, uid: int | None = None)` with `path() -> Path`, `install(spec: ServiceSpec) -> Path`, `start() -> None`, `stop() -> None`, `is_running() -> bool`, `remove() -> None`.
- macOS commands: `launchctl bootstrap gui/<uid> <plist>`, `launchctl kickstart -k gui/<uid>/<label>`, `launchctl bootout gui/<uid>/<label>`, and `launchctl print gui/<uid>/<label>` (running when the output contains `state = running`). Linux: `systemctl --user daemon-reload`, `systemctl --user enable --now <unit>`, `restart`, `stop`, `is-active <unit>` (output `active`), `disable --now`.

- [ ] **Step 1: Write the failing tests:** the plist parses back with `EnvironmentVariables["HOST"] == "127.0.0.1"`, `["PORT"] == "3001"` and `ProgramArguments == spec.program`; the systemd text quotes a workdir and a program path that contain a space; `install` writes `<home>/Library/LaunchAgents/<label>.plist` on macOS and `<home>/.config/systemd/user/<unit>` on Linux and issues the commands above in order (a fake `run` records argv); `is_running` reads both outputs; `remove` deletes the file after stopping.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add service files"

### Task 13: FreeLLMAPI manager

**Files:**
- Create: `plugins/polytropos/freellm.py`
- Test: `tests/test_freellm.py`

**Interfaces:**
- Consumes: `ServiceManager`, `ServiceSpec` (Task 12); `paths.data_dir()`, `paths.state_dir()` (Task 3).
- Produces:
  - `REPO_URL`, `PORT = 3001`, `@dataclass(frozen=True) class Pin: tag: str; commit: str`
  - `@dataclass(frozen=True) class Detected: listening: bool; is_freellmapi: bool; healthy: bool; managed: bool`. `is_freellmapi` is true when `GET http://127.0.0.1:3001/v1/models` answers JSON with status 200 or 401. `healthy` is status 200.
  - `@dataclass(frozen=True) class UpdateResult: ok: bool; from_tag: str; to_tag: str; rolled_back: bool; message: str`
  - Exceptions `BuildError`, `PortBusy`, `NodeMissing`.
  - `class FreeLLM(*, root: Path | None = None, run=subprocess.run, services: ServiceManager | None = None, http_get: Callable[[str, dict], tuple[int, bytes]] | None = None, node: str | None = None)`. `root` defaults to `data_dir() / "freellmapi"` and holds `releases/`, `current` (a symlink), `data/` and `backups/`.
  - Methods:
    - `detect() -> Detected`
    - `node_ok() -> tuple[bool, str]` (`node --version` against `>=20.18.0 <25.0.0`)
    - `build(pin: Pin) -> Path`: `git clone --depth 1 --branch <tag> REPO_URL releases/<tag>`, check `git rev-parse HEAD == pin.commit`, replace `server/data` with a symlink to `data/`, `npm ci`, `npm run build`. A failed step removes the release folder and raises `BuildError`.
    - `ensure_env() -> Path` makes sure `data/.env` (mode `0o600`) has an `ENCRYPTION_KEY` line and leaves an existing one unchanged. A new value is the content of `data/.encryption-key` when that file exists, else `secrets.token_hex(32)`. Reason: FreeLLMAPI requires `ENCRYPTION_KEY` when `NODE_ENV=production` and prefers it over the key file (`server/src/lib/crypto.ts`), and a development install keeps its key in `.encryption-key`, so a fresh random key next to an adopted database would make every stored provider key unreadable. The key value is never printed or logged.
    - `install(pin: Pin) -> None`: raises `PortBusy` when `detect().listening` and the service is not managed; builds, runs `ensure_env`, points `current`, installs and starts the service with `ServiceSpec(program=[node, str(root / "current/server/dist/index.js")], env={"PORT": "3001", "HOST": "127.0.0.1", "NODE_ENV": "production", "FREEAPI_ENV_PATH": str(root / "data/.env")}, workdir=root / "current", log_path=state_dir() / "freellmapi.log")`, and waits up to 60 s for `detect().is_freellmapi`.
    - `adopt(source_checkout: Path, pin: Pin) -> None`: refuses with `PortBusy` while port 3001 still answers (the user stops the old server first); copies `server/data/freeapi.db`, `server/data/.encryption-key` and `.env` from the checkout into `data/` (each when present), then runs `install(pin)`.
    - `backup(label: str) -> Path` copies `freeapi.db`, `.encryption-key` and `.env` from `data/` into `backups/<time>-<label>/` and keeps the newest 5.
    - `update(pin: Pin, *, check: Callable[[], bool] | None = None) -> UpdateResult`: the six update steps of the spec; on a failed health check or `check()` it restores the backup, points `current` back and starts the old release.
    - `rollback() -> UpdateResult` restores the newest backup and the release it names.
    - `health(api_key: str | None = None) -> bool`
    - `cli_command(lock: dict) -> list[str]`: `[node, str(root / "current/cli/dist/index.js")]` when managed (the `bin` entry of FreeLLMAPI's `cli/package.json`; the root `npm run build` builds server, cli and client), else `["npx", f"freellmapi@{lock['freellmapi']['cli_version']}"]`.

- [ ] **Step 1: Write the failing tests** (a fake `run` that creates folders and returns scripted output, a fake `http_get`, a fake `ServiceManager`)

```python
def test_detect_port_busy_with_other_program():   # HTML 404 on /v1/models -> listening True, is_freellmapi False
def test_install_refuses_when_port_taken_by_unmanaged():
def test_build_verifies_commit():                 # rev-parse returns another sha -> BuildError, release folder removed
def test_build_links_data():                      # releases/<tag>/server/data is a symlink to data/
def test_ensure_env_once():                       # a second call keeps the first key; mode 0o600
def test_ensure_env_reuses_key_file():            # data/.encryption-key holds a key -> .env gets that same value
def test_ensure_env_keeps_existing_key():         # .env already has ENCRYPTION_KEY=x -> unchanged, other lines kept
def test_backup_prunes_to_five():
def test_update_success_switches_current():
def test_update_failed_health_rolls_back():       # current points at the old tag, backup restored, rolled_back True
def test_adopt_copies_data_and_refuses_while_running():
def test_node_range():                            # v20.18.0 ok; v25.1.0 and v18.0.0 refused
```

- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 4b: Check the data symlink against the real server (spec spike 5).** Only when `sysctl -n kern.memorystatus_vm_pressure_level` prints `1` and free disk is above 10 GB. In a scratch folder, run `build()` for the pinned tag with a real `run`, then start `node server/dist/index.js` from the release with `PORT=3999`, `HOST=127.0.0.1` and `FREEAPI_ENV_PATH=<scratch>/data/.env`. Run `ensure_env()` against `<scratch>/data` first. Expected: the server boots in production mode with the key from `data/.env`, `GET http://127.0.0.1:3999/v1/models` answers with 200 or 401, and `freeapi.db` appears in `<scratch>/data/`. Stop the process and delete the scratch folder. Port 3001 and the maintainer's own checkout stay untouched. Write the result to `docs/spikes/2026-11-19-freellmapi-data-symlink.md`; when the memory or disk condition fails, write that the check was skipped and why.
- [ ] **Step 5:** Commit "Add the FreeLLMAPI manager"

### Task 14: Programs and versions.lock

**Files:**
- Create: `versions.lock`, `plugins/polytropos/programs.py`
- Test: `tests/test_programs.py`

**Interfaces:**
- Produces `versions.lock` (TOML). Look the values up now and write each license in a comment next to its section:
  - `[freellmapi]` `tag`, `commit` (`git ls-remote --tags https://github.com/tashfeenahmed/freellmapi`), `node = ">=20.18.0 <25.0.0"`, `cli_version` (`npm view freellmapi version`).
  - `[rtk]` `version`, `repo`, and `[rtk.assets.<darwin-arm64|darwin-x86_64|linux-x86_64|linux-aarch64>]` with `url` and `sha256` (find the repository from `rtk --help` or its Homebrew formula, then `GH_TOKEN=$(gh auth token --user Lil-Clanky) gh release view -R <repo> --json assets`; compute checksums when the release has none).
  - `[graphify]` `package = "graphifyy"`, `version` (`pip index versions graphifyy`). `[ast-grep]` `package = "ast-grep-cli"`, `version`.
  - `[stack-canary]` `repo = "https://github.com/Lil-Clanky/stack-canary"`, `ref` (the commit of Task 19's branch until it is released).
  - `[ollama]` `version`: the newest version the published matrix marks as passing for the default local model.
  - `[optional.opencode]`, `[optional.qmd]`, `[optional.context7]`: package or release channel and version. Note in a comment what is known about qmd's memory use and whether context7 needs a key.
  - `[plugins.ponytail]` and `[plugins.superpowers]`: `repo` and 40-character `commit`.
- Produces `programs.py`:
  - `@dataclass(frozen=True) class Program: name: str; version: str; version_cmd: list[str]; install_cmds: list[list[str]]; optional: bool = False`
  - `load_lock(path: Path) -> dict` (`tomllib`), `programs(lock: dict, *, platform: str, machine: str) -> list[Program]`, `installed_version(p: Program, run=subprocess.run) -> str | None` (first `\d+\.\d+\.\d+` in the output), `needs_install(p: Program, run=subprocess.run) -> bool`, `install(p: Program, run=subprocess.run, *, download: Callable[[str, Path], None] | None = None) -> None`.
  - Default set: rtk (download the asset, verify SHA-256, place it at `~/.local/bin/rtk` with mode `0o755`, then `rtk init --agent hermes`); graphify (`uv tool install graphifyy==<v>`, then `graphify install --platform hermes`); ast-grep (`uv tool install ast-grep-cli==<v>`, then `hermes skills install official/software-development/ast-grep`); stack-canary (`uv tool install git+<repo>@<ref>`).
  - Optional set: OpenCode (its pinned channel, then the FreeLLMAPI CLI `setup-opencode`, then `hermes skills install official/autonomous-ai-agents/opencode`), qmd (pinned channel, then `hermes skills install official/research/qmd`), context7 (pinned package, added to the Hermes MCP config through Hermes's own MCP command when it exists, else printed as a manual step), Ollama (only when missing: the official installer for the platform at the lock version).
  - `ponytail_fallback(hermes_home: Path, lock: dict, run=subprocess.run) -> None`: `git clone` of the pinned commit into `<hermes_home>/plugins/ponytail`, then `hermes plugins enable ponytail`.

- [ ] **Step 1: Write the failing tests:** the lock parses; `programs()` picks the right rtk asset per platform and machine; `installed_version` parses `rtk 1.2.0` and `ast-grep 0.39.1`; a SHA-256 mismatch raises and leaves no file in `~/.local/bin`; a matching installed version is skipped; optional items have `optional=True`; `ponytail_fallback` issues clone, checkout of the pinned commit, and enable in order.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add pinned programs"

### Task 15: Doctor checks

**Files:**
- Create: `plugins/polytropos/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: Tasks 3 to 6 (`load_config`, `cloud_slots_below_floor`), 11, 13, 14.
- Produces:
  - `@dataclass(frozen=True) class CheckResult: status: str; label: str; detail: str = ""; fix: str = ""` with `status` in `ok | warn | fail | info`.
  - `@dataclass class DoctorContext: hermes_home: Path; home: Path; platform: str; lock: dict; pack: dict; run: Callable; http_get: Callable; hermes_version: str | None; tested_hermes: tuple[str, ...] = ("0.23.2",)`
  - Check functions, each `(ctx: DoctorContext) -> list[CheckResult]`:
    - `check_hermes`: tested, newer untested (warn) or missing.
    - `check_plugins`: reads `<hermes_home>/plugins/.install-metadata.json` and compares commits with the pack.
    - `check_freellmapi`: not listening gives `fail` with the install command; listening without being FreeLLMAPI gives `fail` naming port 3001; unhealthy gives `warn`; the mode comes from `state.json`.
    - `check_router`: config errors from `load_config`, cloud slots below 64,000, upstreams whose `/models` does not answer.
    - `check_old_router_section`: a top-level `router:` in `config.yaml` gives `info` that Polytropos ignores it.
    - `check_programs`, `check_canary` (local slots with a `"fail"` verdict give `warn` with the model, the version and a passing version).
    - `check_fit`: RAM from `sysctl -n hw.memsize` or `/proc/meminfo`; local slots with a window above the advice give `warn`. Advice for 16 GB: 3B or smaller with an 8K window.
    - `check_key_files`: the top level of `~/Downloads`, `~/Desktop` and `~`, names matching `*key*.json`, `*keys*.json`, `*.env.bak`, `.env.backup*` or `*.env.backup*`; reports names and permissions and never opens the files.
    - `check_profiles`: `<hermes root>/profiles/*` as `info`, with whether Polytropos is installed there.
    - `check_ponytail`: when installed, `info` about the per-turn cost in gateway chats and the `PONYTAIL_DEFAULT_MODE=off` setting.
    - `check_superpowers`: when installed, `info` that its bootstrap after compaction is not verified on this Hermes version.
    - `check_sleep`: on macOS, `pmset -g` with `sleep` above 0 while a gateway LaunchAgent exists gives `warn` with the `sudo pmset -a sleep 0` command for the user to run.
  - `run_checks(ctx: DoctorContext) -> list[CheckResult]`: every check; a check that raises becomes a `fail` naming the check.
  - `format_report(results: list[CheckResult]) -> str`: one line per result in the layout of the spec's example (`  ok    <label>`), with `detail` after the label and `fix` on an indented next line.

- [ ] **Step 1: Write the failing tests:** one per check, with fixtures for its inputs; port taken by another program (Review Focus 3); a broken router section gives `fail` with the error text (Review Focus 4); the key file check never opens a matched file (patch `builtins.open` and `Path.read_text` to fail for those paths); a raising check becomes one `fail` line; `format_report` matches the spec's example layout.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the doctor checks"

### Task 16: Hooks and sleep hold

**Files:**
- Create: `plugins/polytropos/hooks.py`, `plugins/polytropos/awake.py`
- Test: `tests/test_hooks_awake.py`

**Interfaces:**
- Consumes: `bridge.remember_turn`, `bridge.message_text` (Task 3), `guard.read_signals`, `guard.judge` (Task 4), Task 2's findings on `pre_llm_call` kwargs.
- Produces:
  - `class AwakeHolder(*, platform: str = sys.platform, popen=subprocess.Popen, max_hold_s: int = 7200)` with `acquire(key: str) -> None`, `release(key: str) -> None` and property `active -> int`. The first acquire starts `["caffeinate", "-i", "-t", str(max_hold_s)]` on macOS or `["systemd-inhibit", "--what=idle:sleep", "--who=polytropos", "--why=Hermes turn", "sleep", str(max_hold_s)]` on Linux; the last release terminates it. A key acquired twice counts once. Thread-safe.
  - `on_pre_llm_call(session_id, user_message, conversation_history, is_first_turn, model, platform, **kwargs) -> dict | None`: remembers the turn in the bridge, acquires the hold when `awake` is on, and returns `{"action": "block", "message": BLOCK_TEXT.format(reason=...)}` when the session runs on a local provider without the router and `judge(read_signals())` refuses. "Local provider" uses `kwargs["provider"]` and `kwargs["base_url"]` when Task 2 found them, else `config.yaml` `model.provider` and `model.base_url` (a loopback host on a port other than 3001). Exceptions inside the hook are caught and logged, and the hook returns `None`.
  - `on_post_llm_call(session_id, **kwargs) -> None` and `on_session_end(session_id, **kwargs) -> None` release the hold.
  - Switches in `config.yaml` at `plugins.entries.polytropos`: `guard_hook` (default true) and `awake` (default true).
  - `BLOCK_TEXT = "Polytropos paused this turn because {reason}. A local model can freeze this machine at this level. Switch to a cloud model with /model, or wait and try again."`

- [ ] **Step 1: Write the failing tests:** two sessions on two threads share one hold process, which ends after both release (Review Focus 5); a double acquire and a single release for one key ends the hold; `max_hold_s` reaches the command; a block for a local llama.cpp provider under `warning` pressure and none for `polytropos-router`; a raising guard makes the hook return `None`; the bridge has the turn after the hook.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add hooks and the sleep hold"

### Task 17: CLI, slash command and general plugin entry

**Files:**
- Create: `plugins/polytropos/plugin.yaml`, `plugins/polytropos/cli.py`, `plugins/polytropos/slash.py`
- Modify: `plugins/polytropos/__init__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 11 to 16.
- Produces:
  - `plugin.yaml`: `name: polytropos`, `version: 0.1.0`, `description: Doctor, FreeLLMAPI service, memory guard and sleep hold for Polytropos`, `author: Lil-Clanky`, `provides_hooks: [pre_llm_call, post_llm_call, on_session_end]`, `provides_commands: [polytropos]`, plus the fields Task 2 found necessary.
  - `register(ctx) -> None`: `ctx.register_cli_command("polytropos", "Polytropos doctor, installer and FreeLLMAPI manager", cli.setup_parser, cli.handle)`, `ctx.register_command("polytropos", slash.handle, ...)` in the call shape ponytail uses (`~/.claude/plugins/cache/ponytail/ponytail/5.3.3/__init__.py`), and the three hooks.
  - `cli.setup_parser(parser: argparse.ArgumentParser) -> None` with subcommands `doctor`, `setup`, `install [--all-optional] [--only NAME ...]`, `adopt --from PATH`, `update [freellmapi|programs|all]`, `rollback freellmapi`, `canary run` (filled in by Task 20), `status`, `uninstall [--purge-data]`; `cli.handle(args: argparse.Namespace) -> int`.
  - `setup`: runs `FreeLLM.cli_command(lock) + ["setup-hermes"]`; writes the router section with `hermes config set` (one call per key, each recorded with `record_config_change`); asks before setting `model.provider` and `model.default` to `polytropos-router` and `auto`. In external mode it asks for the FreeLLMAPI URL. When `hermes config set` cannot set a mapping or list value (check `hermes config set --help`), it copies `config.yaml` to `config.yaml.polytropos-backup` and writes the `plugins.entries.polytropos-router` section with `hermes_yaml`, recording the old section in `state.json`.
  - `install`: checks `node_ok()`; installs FreeLLMAPI in managed mode unless `detect()` finds a FreeLLMAPI already, in which case it records external mode and prints the `adopt` command; refuses when port 3001 is held by another program; installs default programs that need it; offers ponytail's fallback when the pack did not install ponytail; asks `Install <name>? [y/N]` per optional item unless `--all-optional` or `--only` decide; records shared parts in `state.json`.
  - `update`: reads `https://api.github.com/repos/Lil-Clanky/polytropos/releases/latest` with `urllib`, prints what changes, asks, then runs `hermes plugins pack install <new pack file>` with inherited stdio, updates programs and runs `FreeLLM.update`. After a plugin change it prints the gateway restart command (check the name with `hermes gateway --help`) and leaves the restart to the user.
  - `uninstall`: stops and removes the service and the programs Polytropos installed when `release_shared` reports no other profile; restores recorded config values with `hermes config set`; prints the `hermes plugins remove` commands for the pack plugins; keeps `data/` unless `--purge-data` and a typed `yes`.
  - `slash.handle(args: str) -> str`: FreeLLMAPI health, the guard verdict, and the last router decision when traces are on.

- [ ] **Step 1: Write the failing tests:** `register` with a fake ctx records the CLI command, the slash command and three hooks; `doctor` prints the report and exits 1 when a check fails; `install` refuses when another program holds port 3001 (Review Focus 3); `install --only qmd` installs only qmd; `uninstall` keeps data without `--purge-data`; `setup` records config changes with their old values. External commands go through an injected runner.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the CLI and the general plugin entry"

### Task 18: stack-canary matrix export

Work in a separate git worktree so the main checkout, which the nightly LaunchAgent uses, stays untouched: `git -C ~/Projects/stack-canary worktree add ~/Projects/polytropos/.worktrees/stack-canary -b feat/matrix-json-openai`. Run its tests with `python -m pytest -q` inside the worktree, with a Python that has stack-canary's test dependencies (check its `pyproject.toml`; create `.venv` inside the worktree with `uv` if needed). Never write to `~/.stack-canary` or the main checkout.

**Files:**
- Modify: the report and publish code in `src/stack_canary/` (find where `MATRIX.md` and `summary.json` are written: `grep -rn "MATRIX.md\|summary.json" src`)
- Test: a new test file under `tests/`

**Interfaces:**
- Produces `matrix.json` next to `MATRIX.md` in the report output and in the published files on the `matrix` branch, in the schema of Task 5: one cell per (Ollama version, model, check), with `passed` and `attempts` counts and a `status` derived the same way `MATRIX.md` derives it. Results of `--openai-endpoint` runs (Task 19) are left out.

- [ ] **Step 1:** Write failing tests: a fixture result set gives the expected cells, including the A6 failure of `qwen3.5:0.8b` on 0.40.16; the publish file list includes `matrix.json`; the sanitizing applied to the other artifacts also covers this file.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The full stack-canary suite passes.
- [ ] **Step 5:** Commit in the worktree "Export the compatibility grid as matrix.json"

### Task 19: stack-canary OpenAI-compatible endpoint mode

Same worktree and branch as Task 18.

**Files:**
- Modify: `src/stack_canary/cli.py` and the Layer A runner
- Test: new tests under `tests/`

**Interfaces:**
- Produces `canary run --openai-endpoint URL [--api-key-env NAME] [--model M ...] [--attempts N] [--summary-json PATH]`. The URL must be a loopback base URL ending in `/v1`. Models come from `--model` or from `GET /v1/models`. Requests carry `Authorization: Bearer <value of NAME>` when `--api-key-env` is given; the value comes from the environment and does not appear in results or logs. The Ollama-specific steps (`/api/version`, `/api/tags`, the memory guard for port 11434, thinking detection) are skipped. Results record `"endpoint_kind": "openai"` and the endpoint URL, in the same JSON layout as `--endpoint` runs. `--attempts` defaults to 1 in this mode. `--summary-json PATH` (also allowed in the other modes) writes `{"endpoint": url, "endpoint_kind": "openai" | "ollama", "results": [{"model": m, "check": "A1", "status": "pass" | "fail" | "error", "passed": n, "attempts": k}]}`.
- Make the `matrix.json` export of Task 18 leave out results with `"endpoint_kind": "openai"`, with a test.

- [ ] **Step 1:** Write failing tests against a fake OpenAI-compatible server: models come from `/v1/models`; the auth header arrives and the key value is absent from the written results; a non-loopback URL is refused; A1 to A6 run once each by default.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The full stack-canary suite passes.
- [ ] **Step 5:** Commit "Add an OpenAI-compatible endpoint mode". Push nothing and tag nothing; the controller reports the branch.

### Task 20: Polytropos canary command

**Files:**
- Modify: `plugins/polytropos/cli.py`
- Test: `tests/test_canary_cli.py`

**Interfaces:**
- Consumes: Task 19's command and result layout; `canary.save_local_results` (Task 5); router config (Task 6); `FreeLLM.update(check=...)` (Task 13).
- Produces `run_canary(cfg: RouterConfig, *, run=subprocess.run, models: list[str] | None = None) -> dict[str, str]`. For each cloud upstream with a loopback `base_url` (others are skipped with a printed note) it runs `canary run --openai-endpoint <base_url> [--api-key-env <api_key_env>] --model <each of its models in the routes, or only those in models> --attempts 1 --summary-json <temp file>`, reads that file, and stores `"<upstream>/<model>" -> "pass"` when checks A1 to A6 all passed, else `"fail"`, with `save_local_results` (merged into earlier results). It returns the new entries. `hermes polytropos canary run` prints a summary and exits 1 when a model fails. `update` passes `lambda: run_canary(cfg, models=[first cloud model]).get(...) == "pass"` as `check`.

- [ ] **Step 1:** Write failing tests with a fake `canary` executable on `PATH` that writes a result file: results saved; exit code 1 when a model fails; the API key value never appears in argv.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the canary command"

### Task 21: whatsapp-human-pace public copy

**Files:**
- Create: `plugins/whatsapp_human_pace/` as a copy of `~/.hermes/plugins/whatsapp-human-pace/` (copy, leave the original untouched)
- Test: `tests/test_whatsapp_human_pace.py`

- [ ] **Step 1:** Read every file of the source plugin. Remove personal data (names, phone numbers, chat ids, paths in a home folder); set `author: Lil-Clanky`; set the description to "Holds incoming WhatsApp messages, answers after a random delay of 5 to 10 minutes, and batches what arrived in the meantime into one turn." List what was removed in the task report.
- [ ] **Step 2:** Write failing tests for a `disclosure` option (config `plugins.entries.whatsapp-human-pace.disclosure`, default `true`; `disclosure_text` default "Replies in this chat come from an assistant."): the note is added to the first reply in a chat each calendar day and to no later reply that day; with `disclosure: false` no note is added.
- [ ] **Step 3:** Implement where the plugin produces or sends the reply, using the hook the plugin already uses.
- [ ] **Step 4:** The tests pass. The plugin stays out of `hermes-pack.yaml`.
- [ ] **Step 5:** Commit "Add the whatsapp-human-pace plugin with a disclosure option"

### Task 22: Pack file, release script and update checker

**Files:**
- Create: `hermes-pack.yaml`, `scripts/release.py`, `scripts/check_updates.py`, `.github/workflows/bump.yml`
- Test: `tests/test_release_tooling.py`

**Interfaces:**
- Produces:
  - `hermes-pack.yaml` as in the spec, with ponytail and superpowers commits from `versions.lock`, own entries with `subdir: plugins/polytropos` and `subdir: plugins/polytropos_router` (or the layout Task 2 found), and `config: {polytropos-router: {default_route: complex}}`.
  - `scripts/release.py`: `bump_pack_refs(pack_path: Path, repo: str, sha: str) -> int` rewrites the `ref` of every entry with that `repo` and returns the count; it rejects a `sha` that is not 40 hex characters. `main()` takes `--sha` (default `git rev-parse HEAD`).
  - `scripts/check_updates.py`: `parse_version(tag: str) -> tuple[int, ...]`, `newer(a: str, b: str) -> bool`, `latest_tag(repo: str, fetch: Callable[[str], bytes]) -> str` (GitHub `releases/latest`, falling back to tags), `update_lock(path: Path, section: str, values: dict[str, str]) -> bool` (rewrites `key = "value"` lines inside one section and keeps comments and other sections), and `main()`, which checks FreeLLMAPI, rtk, graphify, ast-grep, ponytail and superpowers and prints a JSON list of available updates.
  - `bump.yml`: `schedule: cron "17 6 * * 1"` and `workflow_dispatch`; per available update, update `versions.lock`, run the tests, and for FreeLLMAPI also build the new tag, start it on a temporary data folder and check `GET /v1/models`; then open or update one pull request per component with `gh pr create` or `gh pr edit` using `GITHUB_TOKEN` (`permissions: contents: write, pull-requests: write`).

- [ ] **Step 1:** Write failing tests: `bump_pack_refs` rewrites only matching entries and rejects a bad sha; `newer("v0.16.36", "v0.16.9")` is True; `update_lock` keeps comments and other sections; the pack file has 40-hex refs only and no config key matching the secret regex from Global Constraints.
- [ ] **Step 2:** The tests fail. **Step 3:** Implement. **Step 4:** The tests pass.
- [ ] **Step 5:** Commit "Add the pack file and release tooling"

### Task 23: README and reference

**Files:**
- Create: `README.md`, `docs/reference.md`

- [ ] **Step 1:** Write `README.md` for a Hermes user: what Polytropos is, requirements, the install flow of the spec, what the doctor checks, updates and uninstall, what stays on the machine, and the license. Write `docs/reference.md`: every command and option, every config key with its default, every path, the guard defaults (with the untuned Linux PSI value), the `matrix.json` schema, the classifier's extra call per turn, and the excluded items with their reasons.
- [ ] **Step 2:** Run `grep -n '—' README.md docs/reference.md`. Expected: no output. Read both files once against the text rules in Global Constraints.
- [ ] **Step 3:** Commit "Add the README and reference"

### Task 24: End-to-end run in an isolated Hermes home

**Files:**
- Create: `tests/e2e/test_hermes_e2e.py` (marker `e2e`, skipped unless `POLYTROPOS_E2E=1`)

**Interfaces:**
- Consumes: `scripts/hermes_sandbox.py` (Task 2), both plugins, `FakeOpenAI`.

- [ ] **Step 1: Write the tests**

```python
def test_routed_turn_reaches_complex_upstream():  # two fake servers; classifier scripted "complex" -> the complex server got the main request; exit code 0
def test_broken_tool_call_retried_on_next_model(): # first model returns write_file "{bad"; the second a valid call, then text -> Hermes finishes; the second server saw the request
def test_no_launchd_changes():                     # launch_snapshot() equal before and after
def test_doctor_runs_in_sandbox():                 # `hermes polytropos doctor` prints a report and exits 0 or 1 without a traceback
```

- [ ] **Step 2:** Run `POLYTROPOS_E2E=1 ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/e2e -q`. Expected: pass.
- [ ] **Step 3:** Run `python scripts/sync_shared.py --check && python -m pytest -q`. Expected: pass.
- [ ] **Step 4:** Commit "Add the end-to-end tests". The spec's CI install test against Hermes v0.23.2 is added only when Hermes installs from its git tag in a CI job within 10 minutes; otherwise the README's development section says the end-to-end tests run locally.
