# Polytropos design

Date: 2026-11-19
Status: settled for version 1. On 2026-11-19 the maintainer asked for the whole project to be finished in one run without further questions, so the open questions from the builder review were settled as design calls. They are listed under "Calls made after the builder review".

## What this is

Polytropos is a plugin pack for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It sets up an existing Hermes install to run on free cloud models through FreeLLMAPI and on small local models through Ollama, on macOS and Linux.

The work has two halves. The first half picks a suitable model for each turn and keeps local models from overloading the machine. The second half lowers the number of tokens each turn uses. A doctor command installs the pieces at pinned versions and checks that they work together.

The name is the Greek word πολύτροπος, "of many turns". The Odyssey uses it for Odysseus in its first line, and the Homeric Hymn to Hermes uses it for Hermes.

## Why

The maintainer uses Hermes every day, in the terminal and through the messaging gateway, and has built or adopted several pieces that make it cheaper and more reliable on free and local models:

- A model router that classifies each turn as simple or complex and sends it to a matching model (NousResearch/hermes-agent PR 60862, with a UI in PR 61005). Both PRs were closed under the maintainers' `delegation-model-routing` policy, which keeps model choice static per session in core. The code works but has no home.
- FreeLLMAPI (tashfeenahmed/freellmapi), a local server that pools the free tiers of many providers behind one OpenAI-compatible endpoint. It releases very often (v0.16.28 to v0.16.36 between 2026-11-13 and 2026-11-19), so keeping it current by hand is real work.
- stack-canary (Lil-Clanky/stack-canary), which tests tool-call compatibility across Ollama releases. It found that `qwen3.5:0.8b` breaks a `write_file` call on Ollama 0.40.15 and 0.40.16 (ollama/ollama#19694).
- Token-saving tools that already support Hermes: rtk, graphify and ponytail.

Local models have crashed the maintainer's Mac several times when memory pressure and swap ran high. A setup that sends turns to local models has to check the machine first.

Polytropos puts these pieces into one install that works as a whole, so the maintainer can rebuild the setup on another machine and other Hermes users can install the same thing.

## Who it is for

The first user is the maintainer. The pack is built so that any Hermes user on macOS or Linux can install it. Version 1 assumes Hermes is already installed and working.

## Goals

1. An existing Hermes user installs Polytropos with the pack command and the doctor, and can see at any time what is installed and whether it works.
2. Each turn goes to a suitable model. Simple turns go to a small local model or a fast free model, complex turns to a strong free model, with a fallback chain when a model fails.
3. A local model only runs when the machine has room for it. Otherwise the turn goes to a cloud model.
4. FreeLLMAPI runs as a managed service at a pinned version. Updates are checked, backed up and reversible.
5. A tool call that comes back broken is retried on another model.
6. The default set includes the token-saving tools (rtk, ponytail, graphify) and working skills (superpowers, ast-grep).
7. Everything Polytropos installs or changes can be listed and removed again.

## Non-goals

- Installing Hermes itself. Hermes has its own installer.
- Windows support.
- Changes to Hermes core, or a fork of Hermes.
- Managing provider API keys. FreeLLMAPI's dashboard and CLI do that.
- A graphical interface. The router UI from PR 61005 (preset editor and in-chat routing notes) is left for later.
- Copying the code of FreeLLMAPI or other tools into this repository. Polytropos installs them from their own sources.

## Anti-goals

These outcomes would make the project a failure even if every feature works:

- The machine freezes or reboots because Polytropos sent a turn to a local model.
- An update leaves Hermes or FreeLLMAPI broken with no way back.
- Keys or chat text end up somewhere new, such as logs, the repository, published canary data, or a network service the user did not configure.
- Chats get noticeably slower or more expensive than without Polytropos, for example through a slow classifier call on every turn or ponytail's per-turn text in messaging chats.
- A Hermes update breaks the setup and the doctor does not report it.
- The install turns into a script that nobody can follow or undo.

## Constraints

- Reference version: Hermes v0.23.2 (commit ae2a81ccc00). Plugins use manifest v2 (`manifest_version`, `api_version`, `requires_plugins`).
- A pack (`hermes-pack.yaml`) pins each plugin to an exact 40-character commit SHA, optionally with a `subdir`. Pack config may not hold secrets. Installing a pack is interactive: one review confirmation, then consent per plugin capability. There is no `--yes`.
- Core keeps the model static for a session. Per-turn routing has to happen inside a model provider plugin. The session uses one virtual model, and the provider picks the upstream model per turn.
- No Hermes hook can change the model of a turn. `pre_llm_call` can add context to the turn or block it. `pre_api_request` only observes. `pre_tool_call` can block a tool call or change its arguments.
- An installed plugin in `$HERMES_HOME/plugins/<name>/` loads as a model provider only when its `plugin.yaml` sets `kind: model-provider`. The provider can return its own client object from `create_client`.
- FreeLLMAPI needs Node `>=20.18.0 <25.0.0`. In production it runs as `node server/dist/index.js` after `npm run build`, on port 3001. Its database path is fixed at `server/data/freeapi.db` relative to the code, with the encryption key in `server/data/.encryption-key`. It runs its database migrations when it starts. The location of its `.env` can be moved with `FREEAPI_ENV_PATH`. It binds to `::` (all interfaces) unless `HOST` is set.
- `npx freellmapi setup-hermes` writes a custom provider into `$HERMES_HOME/config.yaml` with `api_key: "${FREELLMAPI_API_KEY}"`, puts the key in `$HERMES_HOME/.env` (mode 0600), and makes the FreeLLMAPI model the default.
- macOS uses launchd, `caffeinate`, and `sysctl` for memory pressure (`kern.memorystatus_vm_pressure_level`) and swap (`vm.swapusage`). Linux uses systemd user units, `systemd-inhibit`, and PSI (`/proc/pressure/memory`).
- Plugins and config live per `HERMES_HOME` (per Hermes profile). FreeLLMAPI is one service per machine.
- Hermes refuses to start a session whose model reports a context window below 64,000 tokens (`MINIMUM_CONTEXT_LENGTH` in `agent/model_metadata.py`, checked in `agent/agent_init.py`).
- Pack config seeds may not contain secret-shaped keys. The check matches `token`, `secret`, `password`, `api_key`, `private_key`, `credential` and `auth` anywhere in a key name, so keys such as `max_tokens` and `api_key_env` cannot be seeded through the pack.
- Hermes's contributor rules say plugins use only the published plugin API (`hermes_cli/data/plugin_api.json` and `ctx`).
- A gateway can serve several profiles from one process, so `os.environ` holds the values of the profile that started it.

## Approaches

1. **One repository with a pack and a doctor.** `Lil-Clanky/polytropos` holds its own plugins in subfolders, one `hermes-pack.yaml` that pins its own plugins and the third-party ones (ponytail, superpowers) by commit, and a doctor that installs programs at pinned versions and checks the whole setup. One CI, one release and one issue tracker. The pack file can only point at an earlier commit of its own repository, so each release takes two commits: the code, then the pack file that pins it.
2. **One repository per plugin plus a pack repository.** Each plugin can be listed in the Hermes catalog on its own. Seven repositories mean seven CIs and coordinated releases for every change that crosses plugins, which is too much upkeep for one maintainer at this stage. A plugin can still move out later if it gets users of its own.
3. **An installer that also installs Hermes.** Useful for new users, but version 1 is for people who already run Hermes, and Hermes has its own installer.
4. **A Hermes fork with the router in core.** The maintainers closed the router PR under a standing policy, so a fork would need rebasing on a fast-moving upstream for as long as it exists.

Approach 1 was chosen (design call).

## Decisions

Points marked **Decided** below were settled by the maintainer during the design talk:

- The name Polytropos.
- Built for the maintainer first, and usable by other Hermes users.
- Version 1 targets people who already have Hermes.
- macOS and Linux.
- FreeLLMAPI fully included, managed as a pinned dependency that is easy to keep current.
- The router, stack-canary, FreeLLMAPI, graphify, ponytail and rtk are in.
- The parts and the default and optional sets in this document are in, including every optional item.

Points marked **design call** were made while writing this document and are open to change.

## Calls made after the builder review

The builder review raised six questions. The maintainer was not available, so they were settled as design calls. Each one is described in its own section.

1. Local models and Hermes's 64,000-token minimum: the router reports the cloud models' window and sends a turn to a local model only when the request fits the local model's window (polytropos-router, Guard).
2. Upstreams: version 1 routes to OpenAI-compatible endpoints only. Hermes providers with their own sign-in, such as Codex over OAuth, are reached with `/model` (polytropos-router).
3. ponytail in messaging chats: Polytropos leaves files that ponytail and Hermes own unchanged, and the doctor reports the cost and the setting (Default set).
4. Profiles: plugins and config per profile, with FreeLLMAPI and the programs shared and tracked per profile (Profiles).
5. The maintainer's own FreeLLMAPI stays in external mode until the maintainer adopts it (FreeLLMAPI management).
6. whatsapp-human-pace gets a disclosure option that is on by default in the public copy (Optional set).

## Design

### Repository layout (design call)

```
polytropos/
  hermes-pack.yaml          own and third-party plugins, pinned by commit
  versions.lock             pinned programs (TOML)
  plugins/
    polytropos/             general plugin: CLI, slash command, guard hook, awake hooks
    polytropos-router/      model provider plugin (kind: model-provider)
    whatsapp-human-pace/    optional plugin, disabled by default
  shared/                   code both plugins use, copied into each by the release script
  scripts/release.py
  tests/
  docs/
```

The pack has two plugins of its own. The router has to be a model provider plugin, which Hermes loads through a different path than general plugins. Everything else (doctor, FreeLLMAPI management, canary data, guard hook and awake) sits in one general plugin with a config switch per feature. That keeps the install to two consent screens for the own plugins.

Both plugins need the guard and the canary verdicts. Plugins are installed into separate folders and cannot import each other reliably, so the release script copies `shared/` into each plugin folder and CI checks that the copies match. Spike 3 checks whether a pip entry point is cleaner.

Pack file sketch:

```yaml
name: polytropos
description: Model routing, FreeLLMAPI and token savings for Hermes Agent
author: Lil-Clanky
version: 0.1.0
plugins:
  - repo: Lil-Clanky/polytropos
    ref: <40-char sha>
    subdir: plugins/polytropos
  - repo: Lil-Clanky/polytropos
    ref: <40-char sha>
    subdir: plugins/polytropos-router
  - repo: DietrichGebert/ponytail
    ref: <40-char sha>
  - repo: obra/superpowers
    ref: <40-char sha>
config:
  polytropos-router:
    default_route: complex
skills: []
```

### Install flow

1. `git clone https://github.com/Lil-Clanky/polytropos`, or download `hermes-pack.yaml` from a release.
2. `hermes plugins pack install polytropos/hermes-pack.yaml`. Hermes shows its review screen and asks for consent per plugin.
3. `hermes polytropos doctor` lists what is missing.
4. `hermes polytropos install` installs the default programs and asks about each optional one.
5. The user adds provider keys in FreeLLMAPI's dashboard at `http://127.0.0.1:3001` or with `npx freellmapi keys add`.
6. `hermes polytropos doctor` runs again. When everything passes, it offers to make `polytropos-router/auto` the default model.

Example doctor output (illustrative):

```
Polytropos doctor
  ok    Hermes v0.23.2 (tested)
  ok    plugins: polytropos, polytropos-router, ponytail, superpowers
  ok    FreeLLMAPI v0.16.36, managed, 127.0.0.1:3001
  warn  Ollama 0.40.16 + qwen3.5:0.8b fails tool check A6 (stack-canary); the router skips it for tool turns
  warn  2 files in ~/Downloads look like key exports (file names checked, contents not opened)
  ok    rtk 1.2.0, graphify, ast-grep, stack-canary
  info  memory pressure normal, swap below 4 GB
```

### polytropos-router

A port of the router from PR 60862 (`agent/router_loop.py`, `agent/router_trace.py`, `hermes_cli/router_config.py` and their tests) into a model provider plugin.

- The session uses provider `polytropos-router` with model `auto`. For Hermes the model stays the same for the whole session, which fits the core policy.
- `create_client` returns a client with the shape of the OpenAI client (`chat.completions.create`, including streaming). Per turn it runs the classifier, picks a tier (`simple` or `complex`), and forwards the request to the first usable model in that tier's chain. The upstreams are OpenAI-compatible chat completions endpoints: FreeLLMAPI, Ollama, LM Studio, a llama.cpp server, or any other base URL with an optional key. The client needs no provider-specific code. Hermes providers with their own sign-in or API format, such as Codex over OAuth, stay outside version 1 routing because reaching them would mean using Hermes internals. Users switch to them with `/model` (design call).
- The decision is fixed for the turn. The client starts a new decision when the last message in a request is a user message, and keeps the decision while the request continues a tool loop. Spike 2 checks this rule against real Hermes message sequences.
- The classifier is a short call (16 output tokens, as in the PR) to a fast model. When it fails or times out, the router uses `default_route`. The channel hints from the PR stay, so messaging chats can default to `simple`.
- Fallback: on a rate limit, a server error or a timeout the router moves to the next model in the chain. FreeLLMAPI already falls back between providers for a single model, so the router's chain mostly covers the step between local and cloud models.
- Broken tool calls: when a response has a tool call whose name is not among the offered tools, or whose arguments do not parse as JSON or lack required fields, the router retries the request once on the next model in the chain. With streaming, the router holds back the tool call part of the stream until it is complete. The PR's stream fallback is the starting point.
- Local models go through the guard first. When the request offers tools, a local model whose canary tool checks fail is skipped (design call).
- Context length (design call): Hermes refuses models below 64,000 tokens, so the provider reports the smallest window among the cloud models in the config, and the doctor warns about cloud models below that floor. A turn goes to a local model only when the estimated size of the request plus room for the reply fits that model's configured window. When a tool loop outgrows the local window, the router moves to the next model in the chain for the rest of the turn.
- Traces (design call): JSONL traces of routing decisions are off by default because they contain user text. When turned on, they are written with mode 0600.
- Config (design call): named upstreams plus a chain per tier. The old `router:` section from PR 60862 is ignored, and the doctor reports it when it finds one. The pack seeds only `default_route`, because Hermes refuses pack keys such as `max_tokens` and `api_key_env`. `hermes polytropos setup` writes the rest through Hermes's config command. FreeLLMAPI's own `auto` model picks an available free model, so the defaults work with whatever provider keys a user has. Example:

```yaml
upstreams:
  freellmapi: {base_url: "http://127.0.0.1:3001/v1", api_key_env: FREELLMAPI_API_KEY}
  ollama: {base_url: "http://127.0.0.1:11434/v1", local: true, runtime: ollama, context_window: 8192}
default_route: complex
classifier:
  model: freellmapi/auto
  max_tokens: 16
routes:
  simple: [ollama/llama3.2:3b, freellmapi/auto]
  complex: [freellmapi/auto]
channel_hints:
  whatsapp: simple
save_traces: false
```

The FreeLLMAPI key comes from `FREELLMAPI_API_KEY`, the variable that `setup-hermes` already writes to `$HERMES_HOME/.env`. The router takes key values from the arguments Hermes passes to `create_client` where Hermes resolves them per profile, and otherwise from the environment variable named in the upstream (see Profiles).

### FreeLLMAPI management

Two modes (design call):

- **Managed**, the default for new installs. Polytropos installs, runs and updates FreeLLMAPI.
- **External**. Polytropos connects to a FreeLLMAPI URL the user gives (Docker, the desktop app, another machine) and leaves its management to the user.

Managed mode:

- Channel (design call): a source build at a pinned release tag. Docker takes memory on macOS that local models need and many Mac users do not have it, and the desktop app updates itself outside the pin. The maintainer also runs a source build today.
- Paths (design call), under `${XDG_DATA_HOME:-~/.local/share}/polytropos/freellmapi/` on both systems:
  - `releases/<tag>/` holds a checkout of the tag, verified against the commit in `versions.lock` and built with `npm ci` and `npm run build`.
  - `current` is a symlink to the active release.
  - `data/` holds `freeapi.db`, `.encryption-key` and `.env`. Each release's `server/data` is a symlink to it, because the server has no setting for its database path. `FREEAPI_ENV_PATH` points at `data/.env`.
  - `backups/` keeps the last five pre-update backups.
- Service: a LaunchAgent on macOS (label `io.github.lil-clanky.polytropos.freellmapi`) and a systemd user unit on Linux (`polytropos-freellmapi.service`). Both run `node current/server/dist/index.js` with `PORT=3001`, `NODE_ENV=production` and `HOST=127.0.0.1`, and restart on failure. Binding to loopback is a design call, since FreeLLMAPI listens on all interfaces by default.
- Node: the doctor checks for a Node version in FreeLLMAPI's range and tells the user how to install one. Polytropos leaves installing Node to the user (design call).
- Hermes connection: `setup-hermes` from the CLI package in the pinned checkout writes the provider and the key, so its dependencies come from FreeLLMAPI's lockfile. In external mode the doctor uses `npx freellmapi@<pinned version> setup-hermes`, which pins the package but not its dependencies. The doctor then offers to switch the default model to the router.
- FreeLLMAPI's model catalog keeps updating itself from its signed feed. Polytropos pins the code; the catalog is data.

Updates, with `hermes polytropos update freellmapi`:

1. Show the new tag and the changelog entries since the pinned one.
2. Build the new tag in `releases/<new tag>/` while the running service stays up.
3. Stop the service and copy `freeapi.db`, `.encryption-key` and `.env` to `backups/<time>-<old>-to-<new>/`.
4. Point `current` at the new release and start the service. The new version's migrations run at this point.
5. Check health: `/v1/models` answers, and stack-canary's tool checks pass on one configured model.
6. If a check fails, stop the service, restore the backup, point `current` back and start the old version. The same steps are available as `hermes polytropos rollback freellmapi`. A rollback loses changes made after the update, and the prompt says so.

Keeping the pin current (design call): a weekly GitHub Actions job in this repository builds the newest FreeLLMAPI tag, starts it against a database from the pinned version, checks `/v1/models`, and opens or updates a pull request that bumps `versions.lock`. The same job checks the other pinned programs and plugins. Users get new pins through Polytropos releases. Nothing updates without the user running `update` (design call).

Existing installs: the doctor detects a FreeLLMAPI that already listens on port 3001, or a checkout started by its own launchd job or systemd unit. It offers two choices. Either it keeps that install in external mode, or it adopts it: back up the data, copy it into `data/`, disable the old job and start the managed service. Tools that talk to port 3001 keep working. Adoption runs only when the user asks for it (design call). On the maintainer's machine FreeLLMAPI runs as a development server controlled by a separate dashboard, so it stays in external mode until the maintainer chooses to adopt it.

### Canary

- stack-canary publishes `summary.json` (the last watch run), `MATRIX.md`, `index.html` and `matrix.svg` on its `matrix` branch. The full grid of verdicts per Ollama version, model and check is only in the Markdown and HTML files, so `canary publish` gets a small JSON export of the grid (`matrix.json`). This change is made and released in the stack-canary repository.
- Polytropos caches that file at `${XDG_CACHE_HOME:-~/.cache}/polytropos/canary-matrix.json`, refreshes it at most once a day, and works offline with the last copy. Cells the matrix has not tested count as unknown, which the router allows and the doctor mentions.
- The doctor warns about local Ollama and model pairs that fail a tool check, and names an Ollama version the matrix marks as passing.
- Checks on FreeLLMAPI models need an endpoint mode in stack-canary for OpenAI-compatible servers. Its checks already send requests to `/v1/chat/completions`, but `canary run --endpoint` first calls Ollama's `/api/version` and `/api/tags`. The new mode lists models through `/v1/models`, reads an `Authorization` header value from an environment variable, and skips the Ollama-specific memory guard.
- `hermes polytropos canary run` runs checks A1 to A6 on each FreeLLMAPI model in the router config, with one attempt per check to save free quota (design call). It runs after each FreeLLMAPI update and on request. The results stay on the machine, because they depend on the user's own keys and providers. The router keeps a FreeLLMAPI model out of tool turns while it fails.
- stack-canary is installed with `uv tool install` at its pinned tag.

### Guard

The guard decides whether a local model may run now.

- Signals: memory pressure, swap in use, free disk where Ollama keeps its models, and what Ollama has loaded (`/api/ps`).
- Defaults, taken from stack-canary: refuse at memory pressure "warning" or worse on macOS, above 4 GB of swap, or below 5 GB of free disk. The Linux PSI threshold gets a starting value that the builder measures and documents.
- Fit check before a load: when the model is not loaded yet, the guard estimates its size plus the cache for the configured context and refuses if it does not fit. The community plugin coffee-vram does fit checks, queueing and eviction for Ollama. Spike 9 decides whether to use it for this part.
- While a local request runs, the guard polls the signals every few seconds. At critical pressure or over the swap limit it cancels the request and unloads the model (`keep_alive: 0`), and the router retries the turn on a cloud model. The user's Ollama server keeps running (design call).
- Local runtimes other than Ollama (LM Studio, a llama.cpp server) get the same machine checks. Unloading only works on Ollama, so for the others the guard cancels the running request and refuses new local turns until the signals recover. The fit check uses Ollama's model data and is skipped with a doctor note for the other runtimes.
- Sessions that use a local model without the router get a `pre_llm_call` hook that blocks the turn under the same conditions, with a message that suggests switching to a cloud model. Spike 6 checks which fields that hook receives.
- The guard code is ported from stack-canary's memory guard, together with its tests (design call). Polytropos then has no run-time dependency on stack-canary's Python package.

### Awake

- At turn start (`pre_llm_call`) Polytropos takes a sleep assertion and releases it at turn end (`post_llm_call`, or `on_session_end` for failed and interrupted turns). macOS uses `caffeinate -i` and Linux uses `systemd-inhibit --what=idle:sleep`.
- One hold per process is shared by concurrent gateway sessions through a counter. A hold ends after at most two hours in case a turn hangs (design call).
- System sleep settings, such as sleeping with the lid closed or `pmset` values, belong to the user. The doctor reports when they would stop the gateway from receiving messages and shows the command to change them. It leaves the settings as they are.

### Doctor

The general plugin registers `hermes polytropos <command>` (with `register_cli_command`) and a `/polytropos` slash command that shows the status in a chat. Commands: `doctor`, `install`, `update`, `rollback`, `canary`, `status` and `uninstall`.

Checks:

- Hermes version against the tested range, with a warning for newer untested versions.
- Pack plugins installed at the pinned commits (from `plugins/.install-metadata.json`) and enabled.
- FreeLLMAPI health, version and mode, and whether the router's upstream models answer.
- Programs present at acceptable versions. A program that is already installed at an acceptable version is used as it is.
- Canary verdicts for the configured local models.
- Model and context fit for the machine's memory. The starting advice for a 16 GB machine (models of 3B or smaller with a local window of 8K tokens) comes from the maintainer's own machine and gets refined with measurements. The router only sends a local model the requests that fit its window.
- Loose key files: names and permissions of files in `~/Downloads`, `~/Desktop` and the home folder that look like key exports or `.env` copies. The doctor suggests moving or deleting them and does not open them.
- Whether the superpowers bootstrap survives compaction on this Hermes version (spike 8).
- ponytail's mode per surface (spike 7).

Polytropos keeps a state file at `${XDG_STATE_HOME:-~/.local/state}/polytropos/state.json` that records what it installed and each config value it changed, with the old value. Config changes go through Hermes's own commands where those exist.

### Profiles (design call)

`hermes polytropos` works on the active profile (`HERMES_HOME`). Plugins, config and the FreeLLMAPI client key live in each profile, while FreeLLMAPI and the programs are shared by the machine. `state.json` lists the profiles that use each shared part, and uninstalling from one profile removes a shared part only when no other profile uses it. All profiles use the same FreeLLMAPI client key. A gateway that serves several profiles shares one environment, which is why the router prefers keys that Hermes resolves per profile. The doctor lists the other profiles and whether Polytropos is installed in them.

### Default set

| Item | What it is | Install (design call) | Pinned by |
|---|---|---|---|
| FreeLLMAPI | Pool of free models behind an OpenAI-compatible API | Managed service, see above | Tag and commit |
| polytropos, polytropos-router | The own plugins | Pack | Commit |
| ponytail | Hermes plugin for shorter replies and smaller code changes | Pack | Commit |
| superpowers | Hermes plugin with working skills | Pack | Commit |
| rtk | Compresses command output | Release binary with checksum, then `rtk init --agent hermes` | Version and checksum |
| graphify | Graph of a codebase or document folder, with a Hermes skill | `uv tool install graphifyy`, then `graphify install --platform hermes` | Version |
| ast-grep | Structural code search, with the official Hermes skill | `uv tool install`, then `hermes skills install official/software-development/ast-grep` | Version |
| stack-canary | Tool-call checks | `uv tool install` from its tag | Tag |

ponytail's install can trip a Hermes install check. When the pack install fails on it, the doctor falls back to `git clone` at the pinned commit into `$HERMES_HOME/plugins/ponytail` and `hermes plugins enable ponytail`.

ponytail injects its rules once per session and later requests carry them, which costs tokens in messaging chats. Its mode comes from `PONYTAIL_DEFAULT_MODE` or `~/.config/ponytail/config.json`, both machine-wide, and `/ponytail` changes it for the whole process. Polytropos leaves files that Hermes or ponytail own unchanged, and ponytail keeps its own default (design call). When Hermes offers a supported way to set the gateway's environment, the doctor offers to set `PONYTAIL_DEFAULT_MODE=off` there. Otherwise it reports the cost and the setting.

### Optional set

`hermes polytropos install` offers these one at a time.

| Item | What it is | Install |
|---|---|---|
| OpenCode with the opencode skill | Coding agent in the terminal, running on FreeLLMAPI | Pinned release, `npx freellmapi@<pin> setup-opencode`, `hermes skills install official/autonomous-ai-agents/opencode` |
| qmd with the qmd skill | Local search over notes and documents | Pinned version, `hermes skills install official/research/qmd` |
| context7 | MCP server with library documentation | Pinned version, added to the Hermes MCP config |
| Ollama | Local model runtime | Only when missing, at a version the canary marks as passing |
| whatsapp-human-pace | Hermes plugin that holds incoming WhatsApp messages, answers after a random delay of 5 to 10 minutes, and batches what arrived in the meantime into one turn | From `plugins/whatsapp-human-pace` in this repository, disabled by default |

whatsapp-human-pace is the maintainer's own plugin and is not public yet. Before it moves into this repository it gets a review for personal data, and its description is rewritten to say what it does. The public copy gets a `disclosure` option, on by default, that adds a short note to the first reply in a chat each day saying the replies come from an assistant (design call). The maintainer's installed copy stays as it is.

### Updates

`hermes polytropos update` checks the latest Polytropos release on GitHub, shows what would change, and applies it after the user agrees. Plugins update through the new pack file, run in the same terminal so the user sees Hermes's review and consent prompts. Programs update through the doctor, and FreeLLMAPI through the backup and rollback steps above.

A plugin change needs a gateway restart. The doctor prints the command and leaves the restart to the user, because it interrupts running chats.

### Uninstall

`hermes polytropos uninstall` stops and removes the FreeLLMAPI service, removes the programs and integrations that Polytropos installed (from the state file), sets the config values it changed back to their old values, and removes the pack plugins. FreeLLMAPI's `data/` folder stays unless the user asks to delete it and confirms.

### Security and privacy

- Provider keys stay in FreeLLMAPI's encrypted database. The client key stays in `$HERMES_HOME/.env`, written by `setup-hermes`. Polytropos code does not open either file. The router reads the key from the environment, and nothing in Polytropos prints or logs key values.
- FreeLLMAPI listens on 127.0.0.1 in managed mode.
- Every download is pinned to a commit, a tag or a checksum.
- Router traces are off by default.
- Canary results for FreeLLMAPI models stay on the machine.
- The doctor's key file check reads file names and permissions only.

### Testing

- Offline tests per plugin against a fake OpenAI-compatible server. stack-canary's `tests/fake_ollama.py` is a starting point.
- The router keeps the PR's tests (`test_router_loop.py`, `test_router_config.py`, `test_router_mode.py`, `test_router_trace.py`), adapted to the provider client.
- CI runs on `ubuntu-latest` and `macos-latest`. Test servers skip the reverse DNS lookup in `HTTPServer.server_bind`, which stalls for about 35 seconds on GitHub's macOS runners (found in stack-canary).
- An install test in CI against Hermes v0.23.2 (pack install, doctor, one routed turn against the fake server), if Hermes installs in CI in reasonable time.
- Runs with real local models stay off CI and follow the guard rules.

## Excluded

| Item | Reason |
|---|---|
| OSS worker from the FreeLLMAPI control panel | The maintainer's own contribution workflow |
| Tailscale remote access | Personal network setup with its own security questions |
| Odysseus | A separate project |
| langfuse | Needs a Langfuse server or account; the router's local traces cover version 1 |
| bubblewrap | Linux-only sandbox, unrelated to model choice and token use |
| Hermes WebUI | Interfaces are out of scope for version 1 |
| advanced-delegation | Overlaps with the core settings `delegation.provider` and `delegation.model`; revisit if subagents need routing |
| nvidia-switchyard, cortiq-hermes-router, kfchow-llm-value-router | Other routers; two routers in one session would conflict |
| ui-ux-pro-max | No working Hermes support |
| markitdown | Document conversion, unrelated to model choice and token use |
| llm-wiki, code-wiki | Overlap with graphify and qmd |
| Serena, codebase-memory-mcp | Overlap with graphify and ast-grep, and keep an index resident in memory |
| grill-me | An interview skill; users can install it from the Skills Hub |
| codex-on-mac | Tied to the maintainer's own ChatGPT account |

## Later

- ToolCall-NL-15, the maintainer's benchmark for tool calling in Dutch versus English messages, as an extra check for models used in Dutch chats.
- The router UI from PR 61005.
- Docker as a second FreeLLMAPI channel for Linux servers.
- Testing new Hermes tags with Polytropos installed, as a stack-canary harness.
- An upstream FreeLLMAPI change that makes the database path configurable, which removes the need for the symlink.
- Routing to Hermes providers with their own sign-in (for example Codex) through a published host API, if Hermes offers one that supports tools and streaming.
- Per-surface modes for ponytail, proposed upstream.

## Risks and spikes

Ordered by how much the answer could change the design.

1. **Router as a provider plugin from a pack subfolder.** Build a minimal `kind: model-provider` plugin whose `create_client` forwards to FreeLLMAPI, install it through a pack from a subfolder, and drive one streamed, tool-using turn in the CLI and the gateway. Check Hermes's transport wrappers (`HERMES_SKIP_TRANSPORT_WRAP`, `HERMES_SKIP_ASYNC_WRAP`). If this does not work, the fallback is a small local OpenAI-compatible proxy that Hermes uses as a custom provider. That works with any Hermes version, but turn boundaries then have to be inferred from requests, and it is one more service to run.
2. **Turn boundaries inside the client.** Check the rule (a new decision on a trailing user message) against tool loops, compaction, interrupted turns, subagents and side questions.
3. **Shared code between the two plugins.** Copying `shared/` at release time works but is clumsy. Check whether Hermes can install a plugin from a pip package with a `hermes_agent.plugins` entry point through a pack.
4. **Tool call retry with streaming.** Holding back the tool call part of a stream must not delay plain text.
5. **FreeLLMAPI data symlink.** Check that the server, its migrations and its backup code all follow `server/data` as a symlink, and that `npm ci` and the build leave it in place.
6. **Fields passed to `pre_llm_call`.** The guard hook needs the turn's provider or base URL, and awake needs a turn id.
7. **ponytail.** Whether the Hermes install check blocks the pack install, and whether ponytail's mode can differ between the terminal and the gateway. If it cannot, propose per-surface modes upstream.
8. **superpowers after compaction.** Its README says Hermes has no post-compaction hook, while v0.23.2 lists `post_compaction` as an observer hook. Check whether the bootstrap survives a long session.
9. **coffee-vram.** Maintenance, license, behaviour with Apple Silicon's shared memory, and network use. Use it for the fit check if it passes.
10. **qmd and context7.** qmd's memory use (it can load local models for search), and whether context7 works without an API key.
11. **Licenses.** graphify, superpowers, qmd and context7 are installed from their own sources, so their licenses only need to allow use. Check them before the first release.
12. **Classifier cost.** One extra call per turn adds latency and uses free quota. Measure it. A cheap rule, such as sending very short messages to `simple`, can skip the call.
13. **FreeLLMAPI release pace.** FreeLLMAPI releases several times a week. The weekly bump PR keeps up, and the pre-update backup covers a migration that goes wrong.

## Left to the builder

- Module layout, names and config keys inside the plugins, keeping the PR's schema where it fits.
- The exact turn detection rule, after spike 2.
- The Linux PSI threshold, measured and documented.
- Wording and layout of the doctor's output.
- How `shared/` reaches the plugins, after spike 3.
- The release script and the CI job details.
