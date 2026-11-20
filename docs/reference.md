# Polytropos reference

This reference covers Polytropos 0.1.0 on Hermes Agent v0.23.2. Paths are shown with their defaults. `$HERMES_HOME` is `~/.hermes`, and `$XDG_DATA_HOME`, `$XDG_STATE_HOME` and `$XDG_CACHE_HOME` are `~/.local/share`, `~/.local/state` and `~/.cache`. For a named Hermes profile the Hermes paths are under `~/.hermes/profiles/<name>/`. The install steps are in the [README](../README.md).

- [Commands](#commands)
- [How the router picks a model](#how-the-router-picks-a-model)
- [The classifier's extra call](#the-classifiers-extra-call)
- [Settings](#settings)
- [Memory guard](#memory-guard)
- [Sleep hold](#sleep-hold)
- [FreeLLMAPI](#freellmapi)
- [Programs and integrations](#programs-and-integrations)
- [Canary data](#canary-data)
- [Files and paths](#files-and-paths)
- [Hermes v0.23.2 notes](#hermes-v0232-notes)
- [Not included](#not-included)
- [For maintainers](#for-maintainers)

## Commands

The polytropos plugin adds `hermes polytropos <command>` to Hermes. Every command works on the active Hermes profile, and `hermes --profile <name> polytropos <command>` works on another one. A question takes `y` or `yes` as yes. Any other answer counts as no, and so does a run without a terminal.

| Command | What it does |
|---|---|
| `doctor` | Checks the setup |
| `setup` | Connects Hermes to FreeLLMAPI and writes the router settings |
| `install [--all-optional] [--only NAME ...]` | Installs FreeLLMAPI, ponytail and the pinned programs |
| `adopt --from PATH` | Runs an existing FreeLLMAPI checkout as the Polytropos service |
| `update [freellmapi \| programs \| all]` | Updates to the latest Polytropos release |
| `rollback freellmapi` | Restores FreeLLMAPI from the backup of its last update |
| `canary run` | Runs the stack-canary tool checks on the cloud models |
| `status` | Shows FreeLLMAPI health, the guard verdict and the last router decision |
| `uninstall [--purge-data]` | Removes what Polytropos installed and restores the settings it changed |

In a chat, `/polytropos` shows the same as `status`.

`doctor`, `setup`, `install` and `adopt` read `versions.lock` from the polytropos plugin folder, or from the repository when the plugin runs from a checkout. When the file is missing they stop with status 1.

### doctor

`hermes polytropos doctor` runs the checks below in this order and prints one line per finding. Each line starts with `ok`, `warn`, `fail` or `info`, and a fix, where there is one, follows on the next line. A check that crashes is reported as a failed check, and the other checks still run. The doctor exits with status 1 when any check fails. It does not read `hermes-pack.yaml`. It builds the list of expected plugins from `versions.lock` and Hermes's install record of the polytropos plugin.

| Check | What it reports |
|---|---|
| Hermes | A `fail` when `hermes --version` shows no version. A `warn` for any version other than 0.23.2. |
| Plugins | Compares Hermes's install records in `~/.hermes/plugins/.install-metadata.json` with the expected plugins. ponytail and superpowers are expected at their commits in `versions.lock`, and polytropos-router at the commit of the installed polytropos plugin. A ponytail cloned by `install` has no install record, so the doctor reads its commit with git. A missing plugin is a `fail` with the fix `hermes polytropos update`, and a plugin at another commit is a `warn`. |
| FreeLLMAPI | Whether something answers on 127.0.0.1:3001, whether it is FreeLLMAPI, whether `/v1/models` is healthy, and the mode Polytropos runs it in. Nothing on the port, or another program on it, is a `fail`. |
| Router | Each error in the `polytropos-router` settings is a `fail`. A cloud model whose window is below 64,000 tokens, and an upstream that does not answer on `<base_url>/models`, are a `warn`. |
| Old router section | An `info` when `config.yaml` has a top-level `router:` section, as written by Hermes's closed router pull request. Polytropos ignores that section. |
| Programs | The pinned programs that are present at their version, as one `ok` line. A missing default program or one at another version is a `warn`, and a missing optional program is an `info`. Ollama only has to be present. |
| Canary | For each local model with `runtime: ollama` in the routes, the stack-canary verdict for the installed Ollama version. A failed tool check is a `warn` that names an Ollama version that passes, when the matrix has one. No verdict is an `info`. Each model that failed `canary run` is a `warn` as well. |
| Fit | Only on machines with 16 GB of RAM or less. A `warn` for each local model whose name says it has more than 3B parameters, such as the `8b` in `llama3.1:8b`, or whose upstream has a window above 8192 tokens. |
| Key files | A `warn` per folder for files in `~/Downloads`, `~/Desktop` and `~` whose names match `*key*.json`, `*keys*.json`, `*.env.bak`, `.env.backup*` or `*.env.backup*`. It lists names and permissions and does not open the files. |
| Profiles | An `info` for each profile in `~/.hermes/profiles/`, saying whether Polytropos is installed there. |
| ponytail | An `info` when ponytail is installed. ponytail adds text to every turn, which costs tokens in gateway chats. Setting `PONYTAIL_DEFAULT_MODE=off` in the gateway's environment turns it off there. Polytropos does not change that setting. |
| superpowers | An `info` when superpowers is installed. Whether its bootstrap still works after a context compaction is not verified on Hermes v0.23.2. |
| Sleep | Only on macOS with a Hermes gateway LaunchAgent (`~/Library/LaunchAgents/ai.hermes.gateway*.plist`). A `warn` when `pmset -g` shows a sleep timer, with `sudo pmset -a sleep 0` as the fix for you to run. |

When no check fails and `model.provider` in `config.yaml` is not `polytropos-router`, the doctor asks `Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N]`. A yes sets `model.provider` to `polytropos-router` and `model.default` to `auto`, records both changes for uninstall and prints `The Polytropos router is now the default model.` Without a terminal the doctor prints `To make the Polytropos router the default model, run: hermes polytropos setup`.

### setup

`hermes polytropos setup` runs these steps.

1. It runs FreeLLMAPI's `setup-hermes`. When Polytropos runs FreeLLMAPI, that is the CLI of the installed release, `node ~/.local/share/polytropos/freellmapi/current/cli/dist/index.js setup-hermes`. Otherwise it is `npx freellmapi@<cli_version> setup-hermes`, with `cli_version` from `versions.lock`. In external mode setup first asks `FreeLLMAPI URL [http://127.0.0.1:3001]: ` and passes the answer with `--url`. `setup-hermes` asks for FreeLLMAPI's unified API key, writes it as `FREELLMAPI_API_KEY` to `~/.hermes/.env`, and replaces the `model` section of `config.yaml` so that FreeLLMAPI's model is the default (`provider: custom`).
2. It asks `Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N]`.
3. It writes the router settings with `hermes config set`, in this order, so that the settings parse after each step. After a yes it also sets `model.provider` to `polytropos-router` and `model.default` to `auto`.

| Key | Value |
|---|---|
| `plugins.entries.polytropos-router.routes.complex` | `["freellmapi/auto"]` |
| `plugins.entries.polytropos-router.routes.simple` | `["freellmapi/auto"]` |
| `plugins.entries.polytropos-router.classifier` | `freellmapi/auto` |
| `plugins.entries.polytropos-router.upstreams.freellmapi` | `{"base_url": "<FreeLLMAPI URL>/v1", "api_key_env": "FREELLMAPI_API_KEY"}` |

Setup records every value it changes, with the old value, in `state.json`. When a key changes twice, the first old value stays. A key that already has the new value is skipped. Hermes drops `model.base_url` and `model.api_mode` when `model.provider` changes, so setup records their old values as well, and uninstall restores them. When a `hermes config set` fails, setup stops and prints the command, so you can run it yourself and then run setup again. Setup exits with status 1 when `setup-hermes` or a write fails, and prints `The router settings are written.` when it is done.

### install

`hermes polytropos install [--all-optional] [--only NAME ...]`

| Option | Meaning |
|---|---|
| `--all-optional` | Also install every optional program, without questions |
| `--only NAME ...` | Install only the named parts, without questions. The names are `freellmapi`, `ponytail`, `rtk`, `graphify`, `ast-grep`, `stack-canary`, `opencode`, `qmd`, `context7` and `ollama`. An unknown name stops the command with status 1. |

Without `--only`, install handles FreeLLMAPI, ponytail and the programs, in that order.

- **FreeLLMAPI.** When port 3001 answers and the answer is not FreeLLMAPI, install stops with status 1. When the Polytropos service already runs, it is kept. When another FreeLLMAPI answers, Polytropos records it in external mode and prints how to adopt it. Otherwise install checks Node, builds the pinned tag, writes `ENCRYPTION_KEY`, starts the service and waits up to 60 seconds for it to answer. See [FreeLLMAPI](#freellmapi).
- **ponytail.** When `~/.hermes/plugins/ponytail` does not exist, install asks `ponytail is not installed. Install it from its pinned commit? [y/N]`. A yes runs `git clone https://github.com/DietrichGebert/ponytail ~/.hermes/plugins/ponytail`, then `git -C ~/.hermes/plugins/ponytail checkout <commit>` with the commit from `versions.lock`, then `hermes plugins enable ponytail`.
- **Programs.** A default program is installed when it is missing or at another version. An optional program is installed after `Install <name>? [y/N]`. Ollama is installed only when no `ollama` is found. See [Programs and integrations](#programs-and-integrations).

Install records each part in `state.json`, with the profile that uses it and whether Polytropos installed it. A program that was already present counts as not installed by Polytropos, so uninstall leaves it. Install also records what the set-up commands of the programs change, such as Hermes skills, rtk's Hermes plugin, graphify's skill, the context7 MCP entry and OpenCode's config. Uninstall works from that record. It records only what changed during the install, so a skill or entry you already had stays yours. When a part fails, install goes on with the next one and exits with status 1 at the end. Run it again after fixing the error. A full run ends with `Install finished. Next, run hermes polytropos setup.`

### adopt

`hermes polytropos adopt --from PATH` turns a FreeLLMAPI checkout that you ran yourself into the Polytropos service.

- Stop the old server first. adopt refuses while port 3001 answers.
- adopt refuses when `~/.local/share/polytropos/freellmapi/data/` already holds FreeLLMAPI data. Clear that folder by hand first.
- It checks Node before it copies anything, so a failed adopt can be run again.
- It copies `server/data/freeapi.db` with its `-wal` and `-shm` files, `server/data/.encryption-key` and the checkout's `.env` into the data folder, and gives the key files mode 0600.
- Then it builds the pinned tag and starts the service the way `install` does, and records managed mode.

The old checkout stays as it is. A launchd job or systemd unit that started the old server is not disabled. Disable it yourself, so it does not start the old server again.

### update

`hermes polytropos update [freellmapi | programs | all]`, with `all` as the default.

1. It reads the latest release from `https://api.github.com/repos/Lil-Clanky/polytropos/releases/latest`, then fetches `hermes-pack.yaml` and `versions.lock` at that tag from `https://raw.githubusercontent.com/Lil-Clanky/polytropos/<tag>/`.
2. It prints `The latest Polytropos release is <tag>.` With `all` it also lists each own plugin that is not at the release's commit, and each other pack plugin that is not installed.
3. When Polytropos runs FreeLLMAPI and the release pins another tag, it prints the installed tag, the new tag and `https://github.com/tashfeenahmed/freellmapi/compare/<installed tag>...<new tag>`. `update programs` skips this. When FreeLLMAPI is already on the pinned tag, `update freellmapi` prints `FreeLLMAPI is already on <tag>.` and stops.
4. It asks `Update the plugins, programs and FreeLLMAPI to the versions of <tag>? [y/N]`. With `programs` it asks `Update the programs to the versions of <tag>? [y/N]`. With `freellmapi` and a new tag it asks `Update FreeLLMAPI from <installed tag> to <new tag>? [y/N]`. Any answer other than yes prints `Nothing changed.`
5. With `all` and a plugin from step 2, it runs `hermes plugins pack install --force <pack file>` in the same terminal, so Hermes shows its review screen. It ignores the exit status, because the ponytail entry always fails on Hermes v0.23.2, and checks its own two plugins afterwards instead. When they are not at the release's commit, update stops with status 1 and changes nothing else.
6. With `all`, it installs ponytail when `~/.hermes/plugins/ponytail` is missing.
7. With `all` or `programs`, it installs the release's versions of the default programs and of the optional programs that `install` recorded for this profile.
8. With `all` or `freellmapi`, it updates FreeLLMAPI when Polytropos runs it. In external mode it prints the pinned tag and leaves the update to you, and when FreeLLMAPI is not installed it says to run `hermes polytropos install` first. See [Updates and backups](#updates-and-backups).
9. When the plugins changed, it prints `Restart the gateway to load the new plugin code: hermes gateway restart`.

Update exits with status 1 when it cannot read the release, when the plugin update does not finish, or when a part fails. It installs ponytail only when ponytail is missing, so an installed ponytail stays at its commit when a release pins a newer one.

### rollback

`hermes polytropos rollback freellmapi` asks `Restore FreeLLMAPI from the backup of its last update? The database and keys go back to that backup, so changes made since the update are lost. [y/N]`. It takes the newest backup. It refuses when that backup belongs to an update other than the one that made the current release, or when the old release is gone from `releases/`. Otherwise it stops the service, restores the data from the backup, points `current` back at the old release and starts it. It exits with status 1 when the rollback did not finish.

### canary run

`hermes polytropos canary run` tests the cloud models in the router's routes with stack-canary. For each cloud upstream whose base URL is on this machine, it runs one stack-canary command with all of that upstream's models.

```sh
canary run --openai-endpoint <base_url> --api-key-env <api_key_env> --model <model> [--model <model> ...] --attempts 1 --summary-json <file>
```

Polytropos passes the name of the upstream's key variable, and stack-canary reads the key from that variable itself. `--api-key-env` is left out when the upstream has no `api_key_env`. A model passes when checks A1 to A6 all pass, with one attempt per check to save free quota. The results are merged into `~/.local/share/polytropos/canary/openai-results.json`. Upstreams on other hosts are skipped with a message, because stack-canary only tests endpoints on this machine. Local upstreams are skipped as well. For local Ollama models the router reads the published matrix.

The command prints one line per model. It exits with status 1 when a model fails or when no model was tested. When the router settings have an error, it tests the default routes. The router uses the results to move a failing model to the end of the chain for turns that offer tools.

### status and /polytropos

`hermes polytropos status`, and `/polytropos` in a chat, print up to three lines.

- FreeLLMAPI on 127.0.0.1:3001, checked with the profile's `FREELLMAPI_API_KEY`.
- The memory guard verdict for local models.
- The last router decision, when `save_traces` is on.

### uninstall

`hermes polytropos uninstall [--purge-data]` works on the active profile.

- It sets each config value that Polytropos changed in this profile back to its old value, newest change first. It uses `hermes config set`, or `hermes config unset` when the key did not exist before. A value that already has its old value is left alone. When a restore fails, uninstall prints the command and keeps the record.
- It undoes the integrations that `install` recorded in this profile. Hermes skills go with `hermes skills uninstall <name> --yes`, and rtk's Hermes plugin with `~/.local/bin/rtk init --agent hermes --uninstall`. The context7 MCP entry is restored with the other config changes. graphify writes its skill to `~/.hermes/skills/graphify/` whatever the profile, so uninstall deletes that folder only in the default profile and prints its path in the others. FreeLLMAPI's `setup-opencode` merged its provider into OpenCode's own config, so uninstall prints what to change there by hand. When an undo command fails, uninstall prints the command and keeps the record.
- It drops this profile from each shared part. When no other profile uses a part and Polytropos installed it, uninstall removes it. For FreeLLMAPI that is the service, `releases/` and the `current` link. Programs go with `uv tool uninstall`, `npm uninstall -g`, or by deleting their files in `~/.local` for rtk and the macOS Ollama. Ollama on Linux came from Ollama's install script, and uninstall asks you to remove it yourself.
- With `--purge-data` it asks `Type yes to delete <path> with FreeLLMAPI's database, keys and backups: ` and deletes `~/.local/share/polytropos/freellmapi` after `yes`. While another profile still uses FreeLLMAPI, the folder stays. Without the option, uninstall prints where the data stays.
- Last, it prints `hermes plugins remove <name>` for each pack plugin in the profile (`polytropos-router`, `ponytail`, `superpowers` and `polytropos`) and asks you to restart the gateway with `hermes gateway restart`.

Uninstall exits with status 1 when a shared part could not be removed. It keeps the record of that part, so the next uninstall tries again.

## How the router picks a model

Hermes keeps one model for a whole session, so the router runs as a model provider. A session uses provider `polytropos-router` with model `auto`, and the router sends each request on to a real model.

### One decision per turn

The router makes one decision per user message. The requests of the tool loop that follows reuse it, because decisions are cached under a hash of all user messages so far. A decision comes from the first rule that applies.

1. **Channel hint.** When the turn came from a gateway platform listed in `channel_hints`, the router takes that route and skips the classifier. The reason is `hint:<platform>`. The polytropos plugin tells the router which platform a turn came from, so channel hints need both plugins.
2. **No classifier.** With the classifier turned off, the router takes `default_route`. The reason is `default`.
3. **Classifier.** The classifier answers `simple` or `complex`. When the answer names a route, the router takes it, with the reason `classifier`. Any other answer, an error or a timeout leads to `default_route`, with the reason `classifier_error`.

### The chain

The chain for a turn is the route's list of models. The `simple` route continues with the models of `complex`, so a simple turn falls back to them when its own models cannot take it. Each model appears once in the chain.

Before each request the router filters the chain.

- A model on a local upstream (`local: true`) must pass the memory guard, and the request must fit its window. The estimate is the length of the request's messages and tool definitions as JSON, divided by 3, plus the request's `max_tokens`, or 1024 when it has none. A local upstream without `context_window` gets no requests.
- On an upstream with `runtime: ollama`, a local model must also pass the fit check. When the check cannot tell, the model is allowed.
- When the request offers tools, a model that failed its tool checks moves to the end of the chain. For a local Ollama model the router looks up the Ollama version that the upstream reports in the stack-canary matrix, and for a cloud model it reads the results of `hermes polytropos canary run`.

Once a model has answered, later requests in the same turn try it first.

### Fallback

On a rate limit (HTTP 429), a connection error or timeout, or a server error (HTTP 5xx), the router tries the next model in the chain. Other errors go straight to Hermes. When every model fails this way, Hermes gets the last error. When no model in the chain passes the filters, the error is `polytropos-router: no model in the route can take this request`.

### Broken tool calls

A tool call is broken when it names a tool the request did not offer, when its arguments are not a JSON object, or when it lacks a required argument. The router then sends the request once more, to the next model in the chain. When that request fails with an error, the router keeps the first answer.

With streaming, text goes to Hermes as it arrives, and the tool-call part of the stream is held until the stream ends. A retry happens only while nothing has reached Hermes yet. A stream that fails with an error also moves to the next model only before any output. After output the error goes to Hermes.

### Watchdog

While a request to a local upstream runs, the router reads the memory signals every 2 seconds. At critical memory pressure, or with swap above the limit, it aborts the request, and on an Ollama upstream it unloads the model with `keep_alive: 0`. A request that has not sent output yet then moves to the next model. The Ollama server itself keeps running.

### Context window reported to Hermes

Hermes refuses a model whose context window is below 64,000 tokens. The router reports the smallest window among the cloud models in its routes, and never less than 64,000. A cloud upstream without `context_window` counts as `cloud_window_default`, which is 128,000. The doctor warns about cloud models below 64,000.

### Keys

For an upstream with `api_key_env: FREELLMAPI_API_KEY`, the router uses the key that Hermes resolved for the profile. For another `api_key_env` it reads that variable through Hermes, from the environment or the profile's `.env`. An upstream without a key gets the placeholder `none`.

### Traces

With `save_traces: true` the router appends one JSON line per request to `~/.local/state/polytropos/router-traces.jsonl`, a file it creates with mode 0600. Each line has `time`, `tier`, `reason`, `slot` (the model that answered), `retried` and `fell_back_from` (the models tried before it), and no message text. `status` and `/polytropos` show the last line.

## The classifier's extra call

With the classifier on, each new user message costs one extra request before the turn starts. By default it goes to `freellmapi/auto` with a fixed system prompt, which is in `plugins/polytropos_router/classifier.py`, and the text of the last user message. It asks for at most 16 output tokens, with `temperature` 0 and a timeout of 8 seconds. Requests later in the same turn reuse the decision, so a tool loop does not repeat the call.

The call uses free quota and adds its latency to the start of every turn. There is no rule that skips it for short messages. A channel hint skips it for the turns of that platform, and `classifier: null` turns it off, which sends every turn to `default_route`. The classifier sees the last user message, so with a cloud classifier that text goes to a cloud provider, also on turns that then run on a local model. A local classifier passes the memory guard and the fit check first, like any local model.

## Settings

### Router

The router reads its settings from `plugins.entries.polytropos-router` in the profile's `config.yaml`. Change them with `hermes config set` or `hermes config edit`. The router reads the settings again for every new client, so a change applies from the next turn.

| Key | Default | Meaning |
|---|---|---|
| `upstreams.<name>.base_url` | required | Base URL of an OpenAI-compatible API, such as `http://127.0.0.1:3001/v1` |
| `upstreams.<name>.api_key_env` | empty | Name of the variable that holds the key |
| `upstreams.<name>.local` | `false` | Marks a model runtime on this machine. Local upstreams go through the memory guard and the window check. |
| `upstreams.<name>.runtime` | empty | `ollama` turns on the fit check, the matrix lookup and unloading |
| `upstreams.<name>.context_window` | `0` | Window in tokens. With `0` a cloud upstream counts as `cloud_window_default`, and a local upstream gets no requests. |
| `routes.<route>` | see below | List of `upstream/model` entries, tried in order. Everything after the first `/` is the model name. |
| `classifier` | `freellmapi/auto` | An `upstream/model` entry, a mapping with `model`, `max_tokens` and `timeout_s`, or `null`, `false` or `""` to turn it off |
| `classifier.max_tokens` | `16` | Output tokens for the classifier |
| `classifier.timeout_s` | `8.0` | Timeout of the classifier call in seconds |
| `default_route` | `complex` | Route used when the classifier is off or gives no usable answer |
| `channel_hints.<platform>` | none | Route for turns from a gateway platform, such as `whatsapp: simple`. These turns skip the classifier. |
| `save_traces` | `false` | Write a trace line per request |
| `cloud_window_default` | `128000` | Window assumed for a cloud upstream without `context_window` |

Without an `upstreams` key the router has one upstream, `freellmapi`, with `base_url: http://127.0.0.1:3001/v1` and `api_key_env: FREELLMAPI_API_KEY`. A missing `routes` then means `simple` and `complex` both `[freellmapi/auto]`, and a missing `classifier` means `freellmapi/auto`. Once you set `upstreams`, it replaces the default upstream, `routes` is required and a missing `classifier` means no classifier.

Every route entry must name a known upstream, and `default_route` and every channel hint must name a route. The classifier only answers `simple` or `complex`, so a route with another name is reached through `default_route` or a channel hint only. When the settings have an error, the router runs on the built-in defaults above, logs a warning, and the doctor shows the error.

Here is an example with a local Ollama model first on the simple route. With `context_window: 8192` that model only gets requests whose estimate fits in 8192 tokens. The estimate includes the tool definitions, so a turn with many tools goes to the next model.

```yaml
plugins:
  entries:
    polytropos-router:
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

The pack seeds `default_route: complex` when that key is absent. Hermes refuses pack seeds whose names look like secrets, which rules out keys such as `max_tokens` and `api_key_env`, so `hermes polytropos setup` writes the other keys.

### polytropos plugin

| Key | Default | Meaning |
|---|---|---|
| `plugins.entries.polytropos.settings.guard_hook` | `true` | Pause turns of sessions that use a local model directly while the memory guard refuses. See [Guard hook](#guard-hook). |
| `plugins.entries.polytropos.settings.awake` | `true` | Keep the machine awake while a turn runs. See [Sleep hold](#sleep-hold). |

For example, `hermes config set plugins.entries.polytropos.settings.awake false` turns the sleep hold off.

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `FREELLMAPI_API_KEY` | Hermes, the router, `status`, `canary run` | FreeLLMAPI's unified API key, kept in the profile's `.env` |
| `HERMES_HOME` | all parts | The profile's Hermes home, `~/.hermes` by default |
| `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` | all parts | Base folders for Polytropos's data, state and cache |
| `PONYTAIL_DEFAULT_MODE` | ponytail | ponytail's mode. The doctor mentions `off` for the gateway. |

### whatsapp-human-pace

The plugin reads two keys from `plugins.entries.whatsapp-human-pace` in `config.yaml`.

| Key | Default | Meaning |
|---|---|---|
| `disclosure` | `true` | Start the first reply in a chat each local day with a note |
| `disclosure_text` | `Replies in this chat come from an assistant.` | The note |

It reads three variables from the gateway's environment, which Hermes loads from the profile's `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `HUMAN_PACE_MIN_SECONDS` | `300` | Shortest delay |
| `HUMAN_PACE_MAX_SECONDS` | `600` | Longest delay |
| `HUMAN_PACE_DISABLED` | empty | `1`, `true`, `yes` or `on` turns pacing off |

`/pace` changes the delay from the terminal or any chat other than WhatsApp. It cannot be used from WhatsApp.

| Command | Effect |
|---|---|
| `/pace` | Shows the current delay and the chats that wait |
| `/pace 1-3` | Random delay of 1 to 3 minutes |
| `/pace 2` | About 2 minutes, from 1.5 to 2.5 |
| `/pace 30s-90s` | Delay in seconds |
| `/pace off` | Replies right away, and answers what waits now |
| `/pace on` | Turns pacing back on |
| `/pace reset` | Back to the delay from the environment |
| `/pace now` | Answers everything that waits now |
| `/pace help` | Shows these commands |

`/pace` writes `enabled`, `min_seconds` and `max_seconds` to `~/.hermes/plugins/whatsapp-human-pace/settings.json`, and these win over the environment. The same file can hold `humanize`, which is `true` by default. With `false`, WhatsApp replies keep the model's Markdown and emoji. While messages wait, their text is saved in `pending.json` in the same folder. After a gateway restart, the saved texts of a chat go along with the next message from that chat.

## Memory guard

The guard decides whether a local model may run now. It reads three signals.

| Signal | macOS | Linux | A local turn is refused when |
|---|---|---|---|
| Memory pressure | `sysctl -n kern.memorystatus_vm_pressure_level`. 1 or lower is normal, 2 and 3 are warning, 4 and higher critical. | `/proc/pressure/memory`. `full avg10` at 10.0 or above is critical. Otherwise `some avg10` at 10.0 or above is warning. | the pressure is warning or critical |
| Swap in use | `sysctl -n vm.swapusage` | `SwapTotal` minus `SwapFree` in `/proc/meminfo` | swap use is above 4 GiB |
| Free disk | at `~/.ollama/models`, or the home folder when that does not exist | the same | free disk is below 5 GiB |

The Linux PSI threshold of 10.0 is an untuned starting value. All limits are fixed in the code and have no setting. A signal that cannot be read counts as unknown and does not block. A refusal names its reason, in the form `memory pressure is <level>`, `swap use is <n> GB` or `free disk is <n> GB`.

### Fit check

For a model on an Ollama upstream that is not loaded yet, the guard adds up the model's size from `/api/tags`, the KV cache for the upstream's `context_window`, the models already loaded (`/api/ps`) and a reserve of 4 GiB. The KV cache is 2 × layers × KV heads × head size × window × 2 bytes, with the numbers from `/api/show`. The model fits when the sum is at most the machine's RAM. A model that is already loaded always fits. When a number cannot be read, the check does not block, and the watchdog stays as the safety net.

### Watchdog

During a request to a local upstream the watchdog reads the signals every 2 seconds. It trips only at critical pressure or with swap above 4 GiB, and free disk does not trip it. See [Watchdog](#watchdog) under the router for what happens next.

### Guard hook

Sessions that use a local model without the router get the guard through a `pre_llm_call` hook. A session counts as local when its provider is not `polytropos-router` and its base URL is on this machine (`localhost` or a loopback address) with a port other than 3001, the FreeLLMAPI port. The hook takes the provider from Hermes's last API request of the session, or from `model` in `config.yaml` when the turn uses the default model. When the provider is unknown, the turn goes ahead. When the guard refuses, the turn ends with this message.

> Polytropos paused this turn because <reason>. A local model can freeze this machine at this level. Switch to a cloud model with /model, or wait and try again.

Set `plugins.entries.polytropos.settings.guard_hook` to `false` to turn the hook off.

## Sleep hold

While a turn runs, the polytropos plugin keeps the machine from going to sleep. On macOS it runs `caffeinate -i -t 7200`, and on Linux `systemd-inhibit --what=idle:sleep --who=polytropos --why="Hermes turn" sleep 7200`. All sessions in one Hermes process share one hold. It ends when the last running turn ends, or after 7200 seconds in case a turn hangs, and the next turn starts a new one. Sleep settings such as `pmset` values stay yours, and the doctor only reports them. Set `plugins.entries.polytropos.settings.awake` to `false` to turn the hold off.

## FreeLLMAPI

### Managed and external mode

Polytropos runs FreeLLMAPI in one of two modes, which it records in `state.json`.

- **Managed.** Polytropos built FreeLLMAPI from its pinned tag and runs it as a service. `install` sets this up on a machine without a FreeLLMAPI, and `adopt` turns an existing checkout into it. `update` changes FreeLLMAPI only in this mode.
- **External.** A FreeLLMAPI already answered on port 3001 when you ran `install`, for example the desktop app, a Docker container or your own checkout. Polytropos uses it as it is and leaves its updates to you. `setup` asks for its URL.

### Files

Managed FreeLLMAPI lives in `~/.local/share/polytropos/freellmapi/`.

| Path | What it holds |
|---|---|
| `releases/<tag>/` | A shallow clone of the tag, checked against the commit in `versions.lock` and built with `npm ci` and `npm run build` |
| `current` | Link to the active release |
| `data/` (mode 0700) | `freeapi.db` with its `-wal` and `-shm` files, `.encryption-key`, and `.env` (mode 0600) |
| `backups/<time>-<old tag>-to-<new tag>/` | Copies of the files in `data/` from before an update. The newest five are kept. |

The service sets `FREEAPI_DB_PATH` to `data/freeapi.db`, and FreeLLMAPI reads its database from there. Each release's `server/data` is also a link to `data/`, the folder where FreeLLMAPI keeps its database and key file by default. The link stays, so both ways lead to the same folder.

### Service

| | macOS | Linux |
|---|---|---|
| Kind | LaunchAgent `io.github.lil-clanky.polytropos.freellmapi` | systemd user unit `polytropos-freellmapi.service` |
| File | `~/Library/LaunchAgents/io.github.lil-clanky.polytropos.freellmapi.plist` | `~/.config/systemd/user/polytropos-freellmapi.service` |
| Start and restart | Starts at login, and starts again after it exits with an error | `Restart=on-failure`, enabled for `default.target` with `systemctl --user enable --now` |
| Output | `~/.local/state/polytropos/freellmapi.log` | The user journal, `journalctl --user -u polytropos-freellmapi` |

Both run `<node> ~/.local/share/polytropos/freellmapi/current/server/dist/index.js` in the `current` folder, where `<node>` is the `node` that was on the `PATH` at install time. The service has a fixed environment.

| Variable | Value |
|---|---|
| `PORT` | `3001` |
| `HOST` | `127.0.0.1` |
| `NODE_ENV` | `production` |
| `FREEAPI_ENV_PATH` | `~/.local/share/polytropos/freellmapi/data/.env` |
| `FREEAPI_DB_PATH` | `~/.local/share/polytropos/freellmapi/data/freeapi.db` |

FreeLLMAPI listens on all interfaces unless `HOST` is set, so the service binds it to 127.0.0.1.

In production FreeLLMAPI needs `ENCRYPTION_KEY`, which encrypts the provider keys in its database. Polytropos writes it into `data/.env` once. It takes the value from `data/.encryption-key` when that file holds one, as after `adopt`, and otherwise makes a random key of 64 hex characters. A key that is already set is never replaced. Keep `data/.env` together with the database. The backups include both.

### Updates and backups

In managed mode, `hermes polytropos update freellmapi`, and `update` with `all`, run these steps.

1. Build the new tag in `releases/<new tag>/` while the running service stays up. A failed build changes nothing.
2. Stop the service and wait up to 30 seconds until port 3001 is free.
3. Copy the files in `data/` to `backups/<UTC time>-<old tag>-to-<new tag>/`, and delete all but the newest five backups.
4. Point `current` at the new release and start the service. FreeLLMAPI runs its database migrations at this start.
5. Check the new version. FreeLLMAPI has to answer `/v1/models` with JSON (status 200 or 401) within 60 seconds, and stack-canary's tool checks have to pass on the first cloud model in the routes whose upstream is on this machine.
6. When a check fails, stop the service, restore the backup, point `current` back at the old release and start it. The restore first deletes the database's `-wal` and `-shm` files, so stale ones cannot damage the restored database.

The check in step 5 needs stack-canary. When stack-canary is missing, or the free model it tests fails a tool check, the update is rolled back. When the router plugin is missing or no route has a cloud model on this machine, only the `/v1/models` check runs. When the old version does not start again after a restore, Polytropos prints the command to start it by hand.

`hermes polytropos rollback freellmapi` runs step 6 by hand, with the newest backup.

## Programs and integrations

The pins are in `versions.lock`. A default program is installed when `<program> --version` shows another version or nothing. Ollama only has to be present.

- **rtk.** Downloads the release archive for this platform (`darwin-arm64`, `darwin-x86_64`, `linux-x86_64` or `linux-aarch64`), checks its SHA-256, installs the binary as `~/.local/bin/rtk`, then runs `~/.local/bin/rtk init --agent hermes`, which adds rtk's Hermes plugin.
- **graphify.** `uv tool install graphifyy==<version>`, then `graphify install --platform hermes`, which writes graphify's skill to `~/.hermes/skills/graphify/`.
- **ast-grep.** `uv tool install ast-grep-cli==<version>`, then `hermes skills install official/software-development/ast-grep`.
- **stack-canary.** `uv tool install git+https://github.com/Lil-Clanky/stack-canary@<commit>`. The pin is a commit, so uv decides whether anything changes.
- **OpenCode**, optional. `npm install -g opencode-ai@<version>`, then `npx -y freellmapi@<cli_version> setup-opencode`, then `hermes skills install official/autonomous-ai-agents/opencode`.
- **qmd**, optional. `npm install -g @tobilu/qmd@<version>`, then `hermes skills install official/research/qmd`. qmd downloads and runs local GGUF models of its own, which is heavy on a machine with little memory.
- **context7**, optional. `npm install -g @upstash/context7-mcp@<version>`, then `hermes mcp add context7 --command context7-mcp`. When `hermes mcp add` fails, install prints it as a manual step and goes on.
- **Ollama**, optional and only when missing. On macOS install downloads `ollama-darwin.tgz` of the pinned version, checks its SHA-256, unpacks it into `~/.local/lib/polytropos/ollama/<version>/` and links `~/.local/bin/ollama` to it. On Linux it runs `curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=<version> sh`. On macOS Polytropos does not start Ollama, so run `ollama serve` yourself.

## Canary data

### The published matrix

The router and the doctor read stack-canary's published results from `https://raw.githubusercontent.com/Lil-Clanky/stack-canary/matrix/matrix.json`. Polytropos keeps a copy in `~/.cache/polytropos/canary-matrix.json` and downloads a new one when the copy is older than 86400 seconds. When the download fails, it uses the old copy, or an empty matrix when there is none.

The file has this form. The example cell is the one in stack-canary's own reference.

```json
{
  "schema": 1,
  "generated": "<ISO 8601 time>",
  "cells": [
    {"ollama": "0.40.16", "model": "qwen3.5:0.8b", "check": "A6", "status": "fail", "passed": 0, "attempts": 3}
  ]
}
```

| Field | Meaning |
|---|---|
| `schema` | Format version. Polytropos reads only `1`. |
| `generated` | When stack-canary wrote the file |
| `cells[].ollama` | Ollama version |
| `cells[].model` | Model name as Ollama knows it |
| `cells[].check` | stack-canary check, `A1` to `A6` |
| `cells[].status` | `pass`, `fail`, `error` or `n/a`. stack-canary reports `flaky` and `timeout` as `fail`. |
| `cells[].passed`, `cells[].attempts` | Passing attempts and recorded attempts |

The file holds no B6 cells and no results of `--openai-endpoint` runs. Polytropos reads `ollama`, `model`, `check` and `status`. A model passes on an Ollama version when A1 to A6 all have `pass`, and it fails when one of them has `fail`. Anything else counts as unknown, which the router allows and the doctor reports as an `info`.

### Local results

`hermes polytropos canary run` stores its verdicts for cloud models in `~/.local/share/polytropos/canary/openai-results.json`. The file is a JSON object that maps `"<upstream>/<model>"` to `"pass"` or `"fail"`. A new run replaces the entries of the models it tested. The results depend on your own keys and providers, so they stay on this machine.

## Files and paths

| Path | What it is |
|---|---|
| `~/.hermes/config.yaml` | Hermes settings. The router reads `plugins.entries.polytropos-router`, and the polytropos plugin reads `plugins.entries.polytropos.settings`. |
| `~/.hermes/.env` | `FREELLMAPI_API_KEY`, written by FreeLLMAPI's `setup-hermes` |
| `~/.hermes/plugins/polytropos/`, `polytropos-router/`, `superpowers/`, `ponytail/` | The pack plugins |
| `~/.hermes/plugins/polytropos/versions.lock` | The copy of `versions.lock` that the commands read |
| `~/.hermes/plugins/.install-metadata.json` | Hermes's install records, which the doctor reads |
| `~/.hermes/plugins/whatsapp-human-pace/` | whatsapp-human-pace with its `settings.json` and `pending.json`, when installed |
| `~/.hermes/skills/graphify/` | graphify's Hermes skill, written by graphify |
| `~/.hermes/plugins/rtk/` | rtk's Hermes plugin, written by `rtk init --agent hermes` |
| `~/.config/opencode/opencode.json` | OpenCode's config, where FreeLLMAPI's `setup-opencode` adds its provider |
| `~/.local/share/polytropos/freellmapi/` | Managed FreeLLMAPI, see [Files](#files) |
| `~/.local/share/polytropos/canary/openai-results.json` | Results of `canary run` |
| `~/.local/state/polytropos/state.json` | What Polytropos installed and changed, mode 0600 |
| `~/.local/state/polytropos/freellmapi.log` | FreeLLMAPI's output on macOS |
| `~/.local/state/polytropos/router-traces.jsonl` | Router traces when `save_traces` is on, mode 0600 |
| `~/.cache/polytropos/canary-matrix.json` | Cached stack-canary matrix |
| `~/Library/LaunchAgents/io.github.lil-clanky.polytropos.freellmapi.plist` | FreeLLMAPI service on macOS |
| `~/.config/systemd/user/polytropos-freellmapi.service` | FreeLLMAPI service on Linux |
| `~/.local/bin/rtk` | rtk |
| `~/.local/bin/ollama`, `~/.local/lib/polytropos/ollama/<version>/` | Ollama on macOS |

### state.json

`~/.local/state/polytropos/state.json` records what Polytropos changed, so that uninstall can undo it. Polytropos writes it with mode 0600. A file that cannot be read is moved aside to `state.json.corrupt`, and Polytropos starts with an empty record.

```json
{
  "version": 1,
  "profiles": {
    "<HERMES_HOME>": {
      "config_changes": [{"key": "model.provider", "old": "<old value>", "new": "polytropos-router"}],
      "integrations": [{"ran": ["hermes", "skills", "install", "official/software-development/ast-grep"],
                        "added": "<HERMES_HOME>/skills/software-development/ast-grep"}]
    }
  },
  "shared": {
    "freellmapi": {"installed_by_us": true, "profiles": ["<HERMES_HOME>"], "version": "<tag>", "mode": "managed"}
  }
}
```

`profiles` is keyed by the profile's Hermes home. `config_changes` lists each changed key with its first old value, `null` when the key did not exist, and its latest new value. An MCP server that `install` added is recorded here too, under `mcp_servers.<name>`. `integrations` lists the other set-up commands that `install` ran, each with the folder or file it added or changed. `shared` has an entry per shared part, `freellmapi` or a program name, with the profiles that use it, whether Polytropos installed it and its version. The FreeLLMAPI entry also has the mode.

## Hermes v0.23.2 notes

- **Pack install.** `hermes plugins pack install` needs a terminal and one `y` at Hermes's review screen. It exits with status 1 when any entry fails. On v0.23.2 Hermes's install check rejects ponytail, so the pack install prints a failure line for ponytail and exits with status 1, also when the other plugins installed. `hermes polytropos install` then installs ponytail with `git clone` at the pinned commit into `~/.hermes/plugins/ponytail`, followed by `hermes plugins enable ponytail`.
- **Reinstall.** Installing the pack over an earlier install needs `--force`.
- **Shallow fetch.** Hermes's shallow git fetch sometimes fails with `fatal: shallow file has changed since we read it`. Running the install again retries it.
- **Plugin folders.** A pack entry with a `subdir` installs under the plugin's manifest name, so `plugins/polytropos_router` becomes `~/.hermes/plugins/polytropos-router/`.
- **Pack config seeds.** A seed lands at the top level of `plugins.entries.<plugin>` and only fills keys that are absent. Hermes refuses seed keys whose names match `(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key|credential|auth)`, and that pattern catches `max_tokens` and `api_key_env`.
- **Router key.** Hermes refuses a turn on the router when `FREELLMAPI_API_KEY` has no value for the profile, also when every route is local. The error starts with `No usable credentials found for provider 'polytropos-router'. Set FREELLMAPI_API_KEY.` When a fallback provider is configured, Hermes walks the fallback chain instead, so the turn runs without the router. `hermes polytropos setup` leaves the key in the profile's `.env`.
- **Context minimum.** Hermes refuses a model that reports a context window below 64,000 tokens. The router reports at least 64,000.
- **Provider switch.** Hermes drops `model.base_url` and `model.api_mode` when `model.provider` changes. Polytropos records both before it switches the provider, so uninstall can restore them.
- **Gateway restart.** A running gateway loads plugin changes only after `hermes gateway restart`. Polytropos prints the command and leaves the restart to you, because a restart interrupts running chats.

## Not included

These were considered for version 1 and left out.

| Item | Reason |
|---|---|
| OSS worker from the FreeLLMAPI control panel | The maintainer's own contribution workflow |
| Tailscale remote access | Personal network setup with its own security questions |
| Odysseus | A separate project |
| langfuse | Needs a Langfuse server or account. The router's local traces cover version 1. |
| bubblewrap | Linux-only sandbox, unrelated to model choice and token use |
| Hermes WebUI | Interfaces are out of scope for version 1 |
| advanced-delegation | Overlaps with the core settings `delegation.provider` and `delegation.model`. It can be revisited if subagents need routing. |
| nvidia-switchyard, cortiq-hermes-router, kfchow-llm-value-router | Other routers. Two routers in one session would conflict. |
| ui-ux-pro-max | No working Hermes support |
| markitdown | Document conversion, unrelated to model choice and token use |
| llm-wiki, code-wiki | Overlap with graphify and qmd |
| Serena, codebase-memory-mcp | Overlap with graphify and ast-grep, and keep an index resident in memory |
| grill-me | An interview skill. Users can install it from the Skills Hub. |
| codex-on-mac | Tied to the maintainer's own ChatGPT account |

## For maintainers

### Releases

A release takes two commits, because the pack file can only pin a commit that already exists.

1. Commit the code.
2. Run `python scripts/release.py`. It sets the `ref` of each `Lil-Clanky/polytropos` entry in `hermes-pack.yaml` to that commit, which is `git rev-parse HEAD` unless you pass `--sha <commit>`. `--repo` picks another repository. The script rewrites the `ref` lines only, and exits with status 1 when no entry matched.
3. Commit the pack file, tag this second commit and publish a GitHub release for the tag. `hermes polytropos update` reads the latest release and takes `hermes-pack.yaml` and `versions.lock` at its tag.

### Pins

`versions.lock` pins FreeLLMAPI (tag, commit, Node range and CLI version), the programs and the third-party plugins. A comment above each section says where its values came from and gives the license. `python scripts/check_updates.py` compares the pins with the newest upstream releases and prints the updates as JSON, and `--lock PATH` reads another lock file. `--apply <component>` writes one update into `versions.lock`. For ponytail and superpowers it also sets their `ref` in `hermes-pack.yaml`. The components are `freellmapi`, `freellmapi-cli`, `rtk`, `graphify`, `ast-grep`, `ponytail` and `superpowers`. The pins of stack-canary, Ollama and the optional programs are moved by hand.

After a change to `versions.lock`, run `python scripts/sync_shared.py`, which copies the file into `plugins/polytropos/`. A pack install copies only the plugin folders, so the commands read that copy.

### The weekly bump job

`.github/workflows/bump.yml` runs every Monday at 06:17 UTC, and you can start it by hand (`workflow_dispatch`). It asks `check_updates.py` which components have an update and starts one job per component. Each job applies its update, runs `sync_shared.py` and the tests without the `e2e` ones, and pushes the branch `bump/<component>`. Then it opens a pull request for that branch, or updates the open one, so there is one pull request per component. For FreeLLMAPI the job also builds the new tag, starts it with an empty database, and checks that `/livez` answers and that `/v1/models` answers 200 or 401 with JSON.

The job needs the repository setting "Allow GitHub Actions to create and approve pull requests", under Settings, Actions, General, Workflow permissions. Without it the job cannot open the pull request.

### Shared code

`shared/polytropos_shared/` holds the code that both plugins use, such as the paths, the memory guard and the canary reader. `python scripts/sync_shared.py` copies it into `plugins/polytropos/_shared/` and `plugins/polytropos_router/_shared/`, and `--check` exits with status 1 when a copy differs. The copies are committed, because Hermes installs a plugin folder straight from git. The test run syncs them at the start, and CI runs `--check` before the tests.

### Tests

CI (`.github/workflows/test.yml`) runs on `ubuntu-latest` and `macos-latest` with Python 3.11 and 3.13. It installs `openai>=2`, `pytest` and `pyyaml`, runs `python scripts/sync_shared.py --check` and then `python -m pytest -q -m "not e2e"`. The router tests run against a fake OpenAI-compatible server on loopback, `tests/fake_openai.py`, and the `tmp_home` fixture points `HOME`, `HERMES_HOME` and the XDG folders at a temporary folder. Tests marked `e2e` need a real Hermes install and run only locally. `scripts/hermes_sandbox.py` builds isolated Hermes homes for them.
