# Polytropos reference

This reference covers Polytropos 0.3.0 on Hermes Agent v0.23.2. Paths are shown with their defaults. `$HERMES_HOME` is `~/.hermes`, and `$XDG_DATA_HOME`, `$XDG_STATE_HOME` and `$XDG_CACHE_HOME` are `~/.local/share`, `~/.local/state` and `~/.cache`. For a named Hermes profile the Hermes paths are under `~/.hermes/profiles/<name>/`. On Windows `~` is `%USERPROFILE%`, `$HERMES_HOME` is `%LOCALAPPDATA%\hermes`, and Polytropos keeps its data, state and cache together in `%LOCALAPPDATA%\polytropos` unless the `XDG_*` variables are set. The install steps are in the [README](../README.md).

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
| `update [freellmapi \| programs \| all]` | Updates the programs and FreeLLMAPI, or one of them, to the versions the installed plugin pins, and checks ponytail |
| `rollback freellmapi` | Restores FreeLLMAPI from the backup of its last update |
| `canary run` | Runs the stack-canary tool checks on the cloud models |
| `status` | Shows FreeLLMAPI health, the guard verdict and the last router decision |
| `uninstall [--purge-data]` | Removes what Polytropos installed and restores the settings it changed |

In a chat, `/polytropos` shows the same as `status`.

`doctor`, `setup`, `install`, `adopt`, `update programs` and `update freellmapi` read `versions.lock` from the polytropos plugin folder, or from the repository when the plugin runs from a checkout. When the file is missing they stop with status 1.

### doctor

`hermes polytropos doctor` runs the checks below in this order and prints one line per finding. Each line starts with `ok`, `warn`, `fail` or `info`, and a fix, where there is one, follows on the next line. A check that crashes is reported as a failed check, and the other checks still run. The doctor exits with status 1 when any check fails. It does not read `hermes-pack.yaml`. It builds the list of expected plugins from `versions.lock` and Hermes's install record of the polytropos plugin.

| Check | What it reports |
|---|---|
| Hermes | A `fail` when `hermes --version` shows no version. A `warn` for any version other than 0.23.2. |
| Plugins | Compares Hermes's install records in `~/.hermes/plugins/.install-metadata.json` with the expected plugins. ponytail and superpowers are expected at their commits in `versions.lock`. polytropos and polytropos-router are expected at the commit in Hermes's install record of the polytropos plugin. Without that record, as when the plugin runs from a checkout, both count as not installed. A missing plugin is a `fail`. Its fix says to install Polytropos again for the own plugins and `hermes plugins install <repo> --ref <commit>` for superpowers. ponytail is the exception, because Hermes does not accept its `plugin.json` yet. A missing ponytail is a `warn` with the fix `hermes polytropos install --only ponytail`, and a ponytail folder without an install record, such as the clone that Polytropos 0.1.1 made, is an `info`. A plugin at another commit is a `warn`. |
| FreeLLMAPI | Whether something answers on 127.0.0.1:3001, or 127.0.0.1:31415 on Windows, and whether it is FreeLLMAPI. Nothing on the port, or another program on it, is a `fail`. Then the doctor asks `/v1/models` with the profile's `FREELLMAPI_API_KEY` and never prints the key. A profile without the key, and a key that FreeLLMAPI refuses with 401, are a `warn` with the fix `hermes config set FREELLMAPI_API_KEY <key>`. Any other result is a `warn`. It names the HTTP status when FreeLLMAPI returned one, and otherwise says that FreeLLMAPI did not answer. The `ok` line shows the mode Polytropos runs FreeLLMAPI in, and in managed mode also the tag of the installed release. |
| Node | `node --version` against FreeLLMAPI's range `>=20.18.0 <25.0.0`. A Node outside the range, or no Node, is a `fail` that says to install Node 22 or 24. In external mode it is an `info`, because Polytropos does not run FreeLLMAPI then, and on Windows it is an `info` as well. |
| Router | Each error in the `polytropos-router` settings is a `fail`. A cloud model whose window is below 64,000 tokens, and an upstream that does not answer on `<base_url>/models`, are a `warn`. A local upstream whose `/api/version` does not answer as Ollama gets an `info` line. The fit check covers Ollama only, so such an upstream gets the memory, swap and disk checks without it. |
| Old router section | An `info` when `config.yaml` has a top-level `router:` section, as written by Hermes's closed router pull request. Polytropos ignores that section. |
| Programs | The pinned programs that are present at their version, as one `ok` line with the version the doctor read for each. A missing default program or one at another version is a `warn`, and a missing optional program is an `info`. rtk is checked at `~/.local/bin/rtk`, or `~/.local/bin/rtk.exe` on Windows, so another rtk on the `PATH` does not count. stack-canary has no version flag. It counts as present at its pin when `canary run --help` works and the commit that `install` recorded in `state.json` is the pinned commit, and its line shows that commit. A stack-canary that runs without such a record is a `warn` that its commit is unknown. Ollama only has to be present, and its line shows the version found. |
| Canary | For each local model with `runtime: ollama` in the routes, the stack-canary verdict for the installed Ollama version. A failed tool check is a `warn` that names an Ollama version that passes, when the matrix has one. No verdict is an `info`. Each model that failed `canary run` is a `warn` as well. |
| Fit | Only on machines with 16 GB of RAM or less. A `warn` for each local model whose name says it has more than 3B parameters, such as the `8b` in `llama3.1:8b`, or whose upstream has a window above 8192 tokens. The warning adds that such a window serves short requests without tools, and that an agent turn with Hermes's default tools needs a window of about 20,000 tokens by the router's estimate. See [Window fit](#window-fit). |
| Key files | A `warn` per folder for files in `~/Downloads`, `~/Desktop` and `~` whose names match `*key*.json`, `*keys*.json`, `*.env.bak`, `.env.backup*` or `*.env.backup*`. It lists names and permissions and does not open the files. |
| Profiles | An `info` for each profile in `~/.hermes/profiles/`, saying whether Polytropos is installed there. |
| ponytail | An `info` when ponytail is installed. ponytail adds text to every turn, which costs tokens in gateway chats. Setting `PONYTAIL_DEFAULT_MODE=off` in the gateway's environment turns it off there. Polytropos does not change that setting. |
| superpowers | An `info` when superpowers is installed. Whether its bootstrap still works after a context compaction is not verified on Hermes v0.23.2. |
| Sleep | Only on macOS with a Hermes gateway LaunchAgent (`~/Library/LaunchAgents/ai.hermes.gateway*.plist`). A `warn` when `pmset -g` shows a sleep timer, with `sudo pmset -a sleep 0` as the fix for you to run. |

When no check fails, `model.provider` in `config.yaml` is not `polytropos-router` and the profile has `FREELLMAPI_API_KEY`, the doctor asks `Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N]`. A yes sets `model.provider` to `polytropos-router` and `model.default` to `auto`, removes `model.context_length` as [setup](#setup) does, records the changes for uninstall and prints `The Polytropos router is now the default model.` Without the key the doctor prints `To use the Polytropos router, store FreeLLMAPI's unified API key with: hermes config set FREELLMAPI_API_KEY <key>`. Without a terminal it prints `To make the Polytropos router the default model, run: hermes polytropos setup`.

### setup

`hermes polytropos setup` runs these steps.

1. It checks that the profile has `FREELLMAPI_API_KEY`, FreeLLMAPI's unified API key. Without it, setup prints `hermes config set FREELLMAPI_API_KEY <key>`, the command that stores the key in the profile's `.env`, and exits with status 1.
2. In external mode it asks `FreeLLMAPI URL [http://127.0.0.1:3001]: `. On Windows the default is the desktop app's port, `http://127.0.0.1:31415` unless the app saved another one.
3. It asks `Make the Polytropos router the default model (provider polytropos-router, model auto)? [y/N]`.
4. It writes the router settings with `hermes config set`, in this order, so that the settings parse after each step. After a yes it also sets `model.provider` to `polytropos-router` and `model.default` to `auto`, and removes `model.context_length` with `hermes config unset`. Hermes v0.23.2 uses a configured `model.context_length` over the window that the provider reports (`agent/agent_init.py:1554-1598` and `agent/model_metadata.py:2218-2220` in Hermes's source). Left in place, it would replace the window that the router reports, see [Context window reported to Hermes](#context-window-reported-to-hermes).

| Key | Value |
|---|---|
| `plugins.entries.polytropos-router.routes.complex` | `["freellmapi/auto"]` |
| `plugins.entries.polytropos-router.routes.simple` | `["freellmapi/auto"]` |
| `plugins.entries.polytropos-router.classifier` | `freellmapi/auto` |
| `plugins.entries.polytropos-router.upstreams.freellmapi` | `{"base_url": "<FreeLLMAPI URL>/v1", "api_key_env": "FREELLMAPI_API_KEY"}` |

Setup records every value it changes, with the old value, in `state.json`. A key that it removes is recorded the same way, so uninstall sets it back. When a key changes twice, the first old value stays. A key that already has the new value is skipped, and so is a key to remove that is not there. `hermes config set model.provider` removes `model.base_url` and `model.api_mode` when they belonged to the previous provider, so setup records their old values as well, and uninstall restores them. When a `hermes config set` or `hermes config unset` fails, setup stops with `Setting <key> failed` or `Removing <key> failed` and prints the command, so you can run it yourself and then run setup again. Setup never writes the `.env` file or the whole `model` section, and never records `model.api_key`. Setup exits with status 1 when the key is missing or a write fails, and prints `The router settings are written.` when it is done.

### install

`hermes polytropos install [--all-optional] [--only NAME ...]`

| Option | Meaning |
|---|---|
| `--all-optional` | Also install every optional program, without questions |
| `--only NAME ...` | Install only the named parts, without questions. The names are `freellmapi`, `ponytail`, `rtk`, `graphify`, `ast-grep`, `stack-canary`, `opencode`, `qmd`, `context7` and `ollama`. An unknown name stops the command with status 1. |

Without `--only`, install handles FreeLLMAPI, ponytail and the programs, in that order.

- **FreeLLMAPI.** When port 3001 answers and the answer is not FreeLLMAPI, install stops with status 1. When the Polytropos service already runs, it is kept. When another FreeLLMAPI answers, Polytropos records it in external mode and prints how to adopt it. Otherwise install checks Node, builds the pinned tag, writes `ENCRYPTION_KEY`, starts the service and waits up to 60 seconds for it to answer. On Windows install builds and checks nothing. It records external mode and prints where to get FreeLLMAPI's desktop app. See [FreeLLMAPI](#freellmapi).
- **ponytail.** When `~/.hermes/plugins/ponytail` does not exist, install asks `ponytail is not installed. Install it from its pinned commit? [y/N]`, and with `--only ponytail` it does not ask. A yes runs `hermes plugins install https://github.com/DietrichGebert/ponytail --ref <commit> --enable` with the commit from `versions.lock`. Hermes scans ponytail first and asks before it installs a plugin with a `caution` verdict. `--enable` enables ponytail without Hermes's own question, and Hermes keeps an install record. Hermes refuses ponytail's `plugin.json` for now, see [Hermes v0.23.2 notes](#hermes-v0232-notes). Install then prints `ponytail was not installed, because Hermes does not accept ponytail's plugin.json yet.` and goes on, and this does not change its exit status. When the folder exists, install leaves ponytail as it is, with or without an install record. When Hermes's install record of ponytail has another commit than `versions.lock`, install prints the command that moves it, `hermes plugins install https://github.com/DietrichGebert/ponytail --ref <commit> --force`, and does not run it.
- **Programs.** A default program is installed when it is missing or at another version. An optional program is installed after `Install <name>? [y/N]`. Ollama is installed only when no `ollama` is found. See [Programs and integrations](#programs-and-integrations).

Install records each part in `state.json`, with the profile that uses it and whether Polytropos installed it. A program that was already present counts as not installed by Polytropos, so uninstall leaves it. Install also records what the set-up commands of the programs change in this profile, such as Hermes skills, rtk's Hermes plugin, graphify's skill, the context7 MCP entry and OpenCode's config. Uninstall works from that record. It records only what changed during the install, so a skill you already had stays yours. An MCP entry that the profile already has stays as it is. Install does not run `hermes mcp add` for it and records nothing.

The programs serve the whole machine, and their integrations belong to each profile. When a program is installed already, for example from another profile, install runs only the set-up commands whose result this profile lacks, such as a Hermes skill, rtk's Hermes plugin or an MCP entry. It prints `<name> is set up for this profile.` and records them for this profile. graphify always writes its skill to `~/.hermes/skills/graphify/`, and OpenCode's config lies outside every profile, so install does not run those two set-up commands again for another profile.

When a part fails, install goes on with the next one and exits with status 1 at the end. Run it again after fixing the error. A full run ends with `Install finished. Next, run hermes polytropos setup.`

### adopt

`hermes polytropos adopt --from PATH` turns a FreeLLMAPI checkout that you ran yourself into the Polytropos service. On Windows, where Polytropos runs no service, adopt changes nothing and exits with status 1.

- Stop the old server first. adopt refuses while port 3001 answers.
- adopt refuses when `~/.local/share/polytropos/freellmapi/data/` already holds FreeLLMAPI data. Clear that folder by hand first.
- It checks the port, the data folder and Node before it copies anything, so an adopt that stops at one of these checks can be run again.
- It copies `server/data/freeapi.db` with its `-wal` and `-shm` files, `server/data/.encryption-key` and the checkout's `.env` into the data folder, and gives the key files mode 0600.
- Then it builds the pinned tag and starts the service the way `install` does, and records managed mode. When the build fails, or the service does not answer, the copied files stay in `data/`, and adopt refuses to run again while they are there. Remove them by hand before you run adopt again. The old checkout keeps its own data, because adopt copies it.

When port 3001 still answers, adopt prints how to stop the old FreeLLMAPI and keep it from starting again. On macOS it shows `launchctl list | grep -i freellm` to find the label of a launchd job, then `launchctl bootout gui/$(id -u)/<label>` and `launchctl disable gui/$(id -u)/<label>`. On Linux it shows `systemctl --user disable --now <unit>`. When a terminal or a dashboard runs the old server, stop it there. adopt never stops or disables anything itself. The old checkout stays as it is.

### update

`hermes polytropos update [freellmapi | programs | all]`, with `all` as the default. Update moves parts to the versions in the `versions.lock` of the installed polytropos plugin, the file that `install` and the doctor read. `all` moves the programs and FreeLLMAPI and checks ponytail, and `programs` and `freellmapi` move one part. Update fetches nothing from the Polytropos repository and never changes the Polytropos plugins. Hermes updates those, with `hermes plugins update` after a catalog install, or `hermes plugins pack install --force` on the pack file of a newer release after a pack install.

1. When Polytropos runs FreeLLMAPI and the pinned tag differs from the installed one, update prints both tags and `https://github.com/tashfeenahmed/freellmapi/compare/<installed tag>...<new tag>`. `update programs` skips this. When FreeLLMAPI is already on the pinned tag, `update freellmapi` prints `FreeLLMAPI is already on <tag>.` and stops.
2. It asks `Update ponytail, the programs and FreeLLMAPI to the versions Polytropos pins? [y/N]`. With `programs` it asks `Update the programs to the versions Polytropos pins? [y/N]`. With `freellmapi` it asks `Update FreeLLMAPI to <tag>? [y/N]`, or `Update FreeLLMAPI from <installed tag> to <new tag>? [y/N]` when Polytropos runs another tag. Any answer other than yes prints `Nothing changed.`
3. With `all`, when `~/.hermes/plugins/ponytail` is missing, it asks `ponytail is not installed. Install it from its pinned commit? [y/N]` and after a yes installs ponytail the way `install` does. When that folder is there, update leaves ponytail as it is and prints the command that moves it when its install record has another commit, as `install` does.
4. With `all` or `programs`, it installs the pinned versions of the default programs and of the optional programs that `install` recorded for this profile. Update adds no Hermes skill, rtk Hermes plugin or MCP entry that this profile lacks, so one that you removed stays removed. `hermes polytropos install` adds them.
5. With `all` or `freellmapi`, it updates FreeLLMAPI when Polytropos runs it. In external mode it prints the pinned tag and leaves the update to you, and when FreeLLMAPI is not installed it says to run `hermes polytropos install` first. See [Updates and backups](#updates-and-backups).
6. With `all`, it ends with a note on how Hermes updates the Polytropos plugins.

Update exits with status 1 when it cannot read `versions.lock` or when a part fails.

### rollback

`hermes polytropos rollback freellmapi` asks `Restore FreeLLMAPI from the backup of its last update? The database and keys go back to that backup, so changes made since the update are lost. [y/N]`. It takes the newest backup. It refuses when that backup belongs to an update other than the one that made the current release, or when the old release is gone from `releases/`. Otherwise it stops the service, restores the data from the backup, points `current` back at the old release and starts it. It exits with status 1 when the rollback did not finish.

### canary run

`hermes polytropos canary run` tests the cloud models in the router's routes with stack-canary. For each cloud upstream whose base URL is on this machine, it runs one stack-canary command with all of that upstream's models.

```sh
canary run --openai-endpoint <base_url> --api-key-env <api_key_env> --model <model> [--model <model> ...] --attempts 1 --summary-json <file>
```

Polytropos passes the name of the upstream's key variable, and stack-canary reads the key from that variable itself. stack-canary runs with the short environment described under [Programs and integrations](#programs-and-integrations), plus that one variable, with the value that Hermes's secret scope gives for this profile. `--api-key-env` is left out when the upstream has no `api_key_env`. A model passes when checks A1 to A6 all pass, with one attempt per check to save free quota. Any other status, and a check without a status, counts as a failure, so a model whose check ends in an `error`, for example from a rate limit, is recorded as `fail`. In the published matrix an `error` counts as unknown, see [The published matrix](#the-published-matrix). The results are merged into `~/.local/share/polytropos/canary/openai-results.json`. Upstreams on other hosts are skipped with a message, because stack-canary only tests endpoints on this machine. Local upstreams are skipped as well. For local Ollama models the router reads the published matrix.

The command prints one line per model. It exits with status 1 when a model fails or when no model was tested. When the router settings have an error, it tests the default routes. The router uses the results to move a failing model to the end of the chain for turns that offer tools.

### status and /polytropos

`hermes polytropos status`, and `/polytropos` in a chat, print up to three lines.

- FreeLLMAPI on 127.0.0.1:3001, or 127.0.0.1:31415 on Windows, checked with the profile's `FREELLMAPI_API_KEY`.
- The memory guard verdict for local models.
- The last router decision, when `save_traces` is on.

### uninstall

`hermes polytropos uninstall [--purge-data]` works on the active profile.

- It sets each config value that Polytropos changed in this profile back to its old value, newest change first. It uses `hermes config set`, or `hermes config unset` when the key did not exist before. Uninstall reads `config.yaml` again before each key, and a key that already has its old value is left alone. `model.provider` comes back before the other `model` keys, because setting it removes `model.base_url` and `model.api_mode`. Uninstall never writes a `model.api_key` back. When `state.json` came from Polytropos 0.1.1, uninstall removes the `${FREELLMAPI_API_KEY}` pointer that FreeLLMAPI's `setup-hermes` wrote to `model.api_key` while that key still holds it. When the section from before that setup had an `api_key`, uninstall says so, and you set it again yourself. When a restore fails, uninstall prints the command and keeps the record. For a mapping, which can hold an API key, it prints where the old value is recorded and leaves the command out.
- It undoes the integrations that `install` recorded for this profile. Hermes skills go with `hermes skills uninstall <name> --yes`, and rtk's Hermes plugin with `~/.local/bin/rtk init --agent hermes --uninstall`. The context7 MCP entry is restored with the other config changes. graphify writes its skill to `~/.hermes/skills/graphify/` whatever the profile, so uninstall deletes that folder only in the default profile and prints its path in the others. FreeLLMAPI's `setup-opencode` added its provider and a default model to OpenCode's config and stored FreeLLMAPI's key under `freellmapi` in `$XDG_DATA_HOME/opencode/auth.json`. Uninstall prints both paths and leaves those changes to you. When an undo command fails, uninstall prints the command and keeps the record.
- It drops this profile from each shared part. When no other profile uses a part and Polytropos installed it, uninstall removes it. For FreeLLMAPI that is the service, `releases/` and the `current` link. Programs go with `uv tool uninstall`, `npm uninstall -g`, or by deleting their files in `~/.local` for rtk and the macOS Ollama. Ollama on Linux and Windows came from Ollama's install script, and uninstall asks you to remove it yourself. On Windows the desktop app is FreeLLMAPI, and uninstall leaves it.
- With `--purge-data` it asks `Type yes to delete <path> with FreeLLMAPI's database, keys and backups: ` and deletes `~/.local/share/polytropos/freellmapi` after `yes`. While another profile still uses FreeLLMAPI, the folder stays. Without the option, uninstall prints where the data stays.
- Last, it prints `hermes plugins remove <name>` for each pack plugin in the profile (`polytropos-router`, `polymetis`, `ponytail`, `superpowers` and `polytropos`) and asks you to restart the gateway with `hermes gateway restart`.

Uninstall exits with status 1 when a restore, an undo or the removal of a shared part failed. It keeps the record of what failed, so the next uninstall tries again.

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

- A model on a local upstream (`local: true`) must pass the memory guard, and the request must fit its window. See [Window fit](#window-fit).
- On an upstream with `runtime: ollama`, a local model must also pass the fit check. When the check cannot tell, the model is allowed.
- When the request offers tools, a model that failed its tool checks moves to the end of the chain. For a local Ollama model the router looks up the Ollama version that the upstream reports in the stack-canary matrix, and for a cloud model it reads the results of `hermes polytropos canary run`.

Once a model has answered, later requests in the same turn try it first.

### Window fit

The router checks the window of local models only. It estimates the size of a request in tokens as the length of the request's messages and tool definitions written as JSON, divided by 3. To that it adds the request's `max_tokens`, or 1024 when the request has none. A local model gets the request when this sum is at most the `context_window` of its upstream. A local upstream without `context_window` gets no requests. When another plugin changes the request for a local model, the check counts the changed request; see [Request hooks for local models](#request-hooks-for-local-models). The code is `_candidates` and `_unusable` in `plugins/polytropos_router/client.py`, with the estimate in `tokens.py`.

Hermes v0.23.2 has no setting for the output limit. Its provider guide says that it no longer reads `model.max_tokens`, `HERMES_MAX_TOKENS` or provider output-cap settings (`website/docs/integrations/providers.md` in Hermes's source), and the router's provider profile sets no output limit of its own. A normal turn therefore reaches the router without `max_tokens`, and the check counts the router's default of 1024 output tokens (`client.py`). The router forwards the request with only `model` changed, so the model gets no `max_tokens` either. When Hermes retries a request, for example after a reply was cut off or an output limit was too large, it can set a one-time `max_tokens`, and the check counts that value.

With `context_window: 8192`, a request fits when its estimate is at most 7168 tokens, which is about 21,500 characters of JSON. Hermes's system prompt and tool definitions count toward that. Hermes's prompt-surface test fixture (`tests/fixtures/prompt_surface/surfaces.json` in Hermes's source) puts a default turn with tools at about 12,000 tokens before any history, which the router's estimate counts as about 16,000. With the 1024 output tokens and the conversation's history on top, an agent turn with Hermes's default tools needs a window of about 20,000 tokens by the router's estimate. A window of 8192 serves short requests without tools, such as titles, and the next model in the chain gets the other requests.

### Request hooks for local models

Another plugin can change the request that a local model gets, for example to leave out tools and skills so that it fits a small window. [Polymetis](https://github.com/Lil-Clanky/polymetis) does this. A plugin registers a hook in the dict `local_request` of a module named `polytropos_router_hooks` in `sys.modules`, which the first plugin that registers creates:

```python
import sys, types

store = sys.modules.setdefault("polytropos_router_hooks", types.ModuleType("polytropos_router_hooks"))
store.__dict__.setdefault("local_request", {})["my-plugin"] = my_hook
```

For each local model in the chain, the router calls the hooks in the order they were registered, with a copy of the request and a dict of `upstream`, `model`, `window` (the model's `context_window`) and `fits`. `fits(request)` tells whether a request fits that window by the router's own estimate. A hook returns `None` to leave the request alone, or `{"request": ..., "variant": ...}` with a new request and a short name for it. The first hook that returns a request wins. The window fit and the request sent to that model then use it, with the `stream` setting of the request the router received, and the trace names the variant: a name of up to 32 letters, digits, `.`, `_` or `-`, else the hook's key. A hook that raises, or returns a request whose messages the router cannot read, is skipped, and the router logs the error type once, without the message. What a hook changes in its copy reaches no other model, so cloud models get the request as the router received it. Without hooks nothing changes.

The memory guard, the fit check and the canary filter work as before. The router judges broken tool calls against the tools of the request it received, because Hermes can run each of those.

### Fallback

On a rate limit (HTTP 429), a connection error or timeout, or a server error (HTTP 5xx), the router tries the next model in the chain, and so it does when a local model rejects a request that a hook changed (HTTP 400 or 422), or the OpenAI SDK refuses that request before sending it. Other errors go straight to Hermes. When every model fails this way, Hermes gets the last error. When no model in the chain passes the filters, the error is `polytropos-router: no model in the route can take this request`.

### Broken tool calls

A tool call is broken when it names a tool the request did not offer, when its arguments are not a JSON object, or when it lacks a required argument. The router then sends the request once more, to the next model in the chain. When that request fails with an error, the router keeps the first answer.

With streaming, text goes to Hermes as it arrives, and the tool-call part of the stream is held until the stream ends. A retry happens only while nothing has reached Hermes yet. A stream that fails with an error also moves to the next model only before any output. After output the error goes to Hermes.

### Watchdog

While a request to a local upstream runs, the router reads the memory signals every 2 seconds. At critical memory pressure, or with swap above the limit, it aborts the request, and on an Ollama upstream it unloads the model with `keep_alive: 0`. A request that has not sent output yet then moves to the next model. The Ollama server itself keeps running.

### Context window reported to Hermes

Hermes refuses a model whose context window is below 64,000 tokens. The router reports the smallest window among the cloud models in its routes, and never less than 64,000. A cloud upstream without `context_window` counts as `cloud_window_default`, which is 128,000. The doctor warns about cloud models below 64,000. A `model.context_length` in `config.yaml` takes the place of this window in Hermes, so `setup` and the doctor remove that key when they make the router the default model.

### Hermes's asynchronous calls

Hermes runs some auxiliary tasks asynchronously, such as vision analysis and plugins that use Hermes's plugin LLM API asynchronously. With the router as the main provider, such a call runs one full routed request, classifier included, and then fails, and Hermes moves on to its next provider. Setting `auxiliary.vision.provider` to another provider avoids this for vision analysis. Hermes reads such a setting per task, as `auxiliary.<task>.provider`. This was read from Hermes's source and has not been tried.

### Keys

For an upstream with `api_key_env: FREELLMAPI_API_KEY`, the router uses the key that Hermes resolved for the profile. For another `api_key_env` it reads that variable through Hermes, from the environment or the profile's `.env`. An upstream without a key gets the placeholder `none`.

### Traces

With `save_traces: true` the router appends one JSON line per request to `~/.local/state/polytropos/router-traces.jsonl`, a file it creates with mode 0600. Each line has `time`, `tier`, `reason`, `slot` (the model that answered), `retried`, `fell_back_from` (the models tried before it), `skipped`, `tokens_est` and `duration_ms`, and no message text. `skipped` lists the local models that the hard filters left out, each with `window`, `guard` or `fit`, and the models that moved to the end of the chain for their tool checks, with `canary`. `tokens_est` is the router's estimate of the request that the answering model got, without the room for the reply. `duration_ms` runs from the start of the request to the answer, or to the end of the stream. When a request hook changed the answering model's request, `shaped` names the variant. At 5 MB the file moves to `router-traces.jsonl.1`, which replaces the older one. `status` and `/polytropos` show the last line.

## The classifier's extra call

With the classifier on, each new user message costs one extra request before the turn starts. By default it goes to `freellmapi/auto` with a fixed system prompt, which is in `plugins/polytropos_router/classifier.py`, and the text of the last user message. A message longer than 2,000 characters is cut to its first 1,000 and its last 1,000 characters. It asks for at most 16 output tokens, with `temperature` 0 and a timeout of 8 seconds. Requests later in the same turn reuse the decision, so a tool loop does not repeat the call.

Hermes's auxiliary calls also pass through the router, because Hermes tries the main provider first for them. Titles, context compression and smart approvals each have a user message of their own, so each of these calls costs one classifier call. The user message of a compression call holds the conversation being summarized, so the classifier gets the start and the end of that conversation.

The call uses free quota and adds its latency to the start of every turn. There is no rule that skips it for short messages. A channel hint skips it for the turns of that platform, and `classifier: null` turns it off, which sends every turn to `default_route`. The classifier sees up to 2,000 characters of the last user message, so with a cloud classifier that text goes to a cloud provider, also on turns that then run on a local model. A local classifier passes the memory guard and the fit check first, like any local model.

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

Without an `upstreams` key the router has one upstream, `freellmapi`, with `base_url: http://127.0.0.1:3001/v1`, or `http://127.0.0.1:31415/v1` on Windows, and `api_key_env: FREELLMAPI_API_KEY`. A missing `routes` then means `simple` and `complex` both `[freellmapi/auto]`, and a missing `classifier` means `freellmapi/auto`. Once you set `upstreams`, it replaces the default upstream, `routes` is required and a missing `classifier` means no classifier.

Every route entry must name a known upstream, and `default_route` and every channel hint must name a route. The classifier only answers `simple` or `complex`, so a route with another name is reached through `default_route` or a channel hint only. When the settings have an error, the router runs on the built-in defaults above, logs a warning, and the doctor shows the error.

Here is an example with a local Ollama model first on the simple route. With `context_window: 8192` that model serves short requests without tools, such as titles, and the other requests go to `freellmapi/auto`. An agent turn with Hermes's default tools needs a window of about 20,000 tokens by the router's estimate, as described in [Window fit](#window-fit).

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
| `FREELLMAPI_API_KEY` | Hermes, the router, `doctor`, `status`, `canary run` | FreeLLMAPI's unified API key, kept in the profile's `.env` |
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

`/pace` writes `enabled`, `min_seconds` and `max_seconds` to `~/.hermes/plugins/whatsapp-human-pace/settings.json`, and these win over the environment. The same file can hold `humanize`, which is `true` by default. With `false`, WhatsApp replies keep the model's Markdown and emoji. While messages wait, their text is saved in `pending.json` in the same folder, with mode 0600. After a gateway restart, the saved texts of a chat go along with the next message from that chat.

### Polymetis

Polymetis reads its settings from `plugins.entries.polymetis.settings` in `config.yaml`, at the start of every turn. With the defaults it only decides and logs. An ablation starts when Hermes next loads the plugin, as after a restart of Hermes or the gateway, because Polymetis adds the middleware it needs then. The keys, the log fields and the report are in [Polymetis's README](https://github.com/Lil-Clanky/polymetis).

## Memory guard

The guard decides whether a local model may run now. It reads three signals.

| Signal | macOS | Linux | Windows | A local turn is refused when |
|---|---|---|---|---|
| Memory pressure | `sysctl -n kern.memorystatus_vm_pressure_level`. 1 or lower is normal, 2 and 3 are warning, 4 and higher critical. | `/proc/pressure/memory`. `full avg10` at 10.0 or above is critical. Otherwise `some avg10` at 10.0 or above is warning. | The memory load from `GlobalMemoryStatusEx`, the percentage of RAM in use. 90 or above is warning, 95 or above critical. | the pressure is warning or critical |
| Swap in use | `sysctl -n vm.swapusage` | `SwapTotal` minus `SwapFree` in `/proc/meminfo` | not read | swap use is above 4 GiB |
| Free disk | at `~/.ollama/models`, or the home folder when that does not exist | the same | the same | free disk is below 5 GiB |

The Linux PSI threshold of 10.0 and the Windows memory loads of 90 and 95 percent are untuned starting values. All limits are fixed in the code and have no setting. A signal that cannot be read counts as unknown and does not block. A refusal names its reason, in the form `memory pressure is <level>`, `swap use is <n> GB` or `free disk is <n> GB`.

### Fit check

For a model on an Ollama upstream that is not loaded yet, the guard adds up the model's size from `/api/tags`, the KV cache for the upstream's `context_window`, the models already loaded (`/api/ps`) and a reserve of 4 GiB. The KV cache is 2 × layers × KV heads × head size × window × 2 bytes, with the numbers from `/api/show`. The model fits when the sum is at most the machine's RAM. A model that is already loaded always fits. The guard compares names without regard to case and reads a name without a tag as `<name>:latest`. When `/api/tags` answers and does not list the model, Ollama has not pulled it. The check then refuses the model and logs a warning that the model is not pulled, once per model while Hermes runs, so the router goes on to the next model in the chain. When `/api/tags` or another number cannot be read, the check does not block, and the watchdog stays as the safety net.

### Watchdog

During a request to a local upstream the watchdog reads the signals every 2 seconds. It trips only at critical pressure or with swap above 4 GiB, and free disk does not trip it. See [Watchdog](#watchdog) under the router for what happens next.

### Guard hook

Sessions that use a local model without the router get the guard through a `pre_llm_call` hook. A session counts as local when its provider is not `polytropos-router` and its base URL is on this machine (`localhost` or a loopback address) with a port other than 3001, the FreeLLMAPI port, or 31415 on Windows. The hook takes the provider from Hermes's last API request of the session, or from `model` in the `config.yaml` of the turn's profile when the turn uses the default model. When the provider is unknown, the turn goes ahead. When the guard refuses, the turn ends with this message.

> Polytropos paused this turn because <reason>. A local model can freeze this machine at this level. Switch to a cloud model with /model, or wait and try again.

Set `plugins.entries.polytropos.settings.guard_hook` to `false` to turn the hook off.

## Sleep hold

While a turn runs, the polytropos plugin keeps the machine from going to sleep. On macOS it runs `caffeinate -i -t 7200`, and on Linux `systemd-inhibit --what=idle:sleep --who=polytropos --why="Hermes turn" sleep 7200`. On Windows it starts Hermes's own Python with a short script that calls `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` and sleeps 7200 seconds, without a console window. The display may still turn off, as with `caffeinate -i`. All sessions in one Hermes process share one hold. It ends when the last running turn ends, or after 7200 seconds in case a turn hangs, and the next turn starts a new one. Sleep settings such as `pmset` values stay yours, and the doctor only reports them. Set `plugins.entries.polytropos.settings.awake` to `false` to turn the hold off.

## FreeLLMAPI

### Managed and external mode

Polytropos runs FreeLLMAPI in one of two modes, which it records in `state.json`.

- **Managed.** Polytropos built FreeLLMAPI from its pinned tag and runs it as a service. `install` sets this up on a machine without a FreeLLMAPI, and `adopt` turns an existing checkout into it. `update` changes FreeLLMAPI only in this mode.
- **External.** A FreeLLMAPI already answered on port 3001 when you ran `install`, for example the desktop app, a Docker container or your own checkout. Polytropos uses it as it is and leaves its updates to you. `setup` asks for its URL.

On Windows the mode is always external. Polytropos builds no FreeLLMAPI there and runs no service. You install FreeLLMAPI's desktop app from the [Microsoft Store](https://apps.microsoft.com/detail/9PBN6G8K2B7F), where it is signed and updates itself, or as the unsigned `.exe` installer from its [releases](https://github.com/tashfeenahmed/freellmapi/releases). The app serves the API on `127.0.0.1:31415` and keeps its data in `%APPDATA%\FreeLLMAPI`. A click on its tray icon opens the dashboard, where you add provider keys, and the Keys page shows the unified API key. When 31415 is taken, the app moves to the next free port and saves it as `port` in `%APPDATA%\FreeLLMAPI\config.json`. The Microsoft Store app keeps its own copy of that file in its package folder, `%LOCALAPPDATA%\Packages\TashAhmed.FreeLLMAPI_<id>\LocalCache\Roaming\FreeLLMAPI\config.json`, because Windows redirects a Store app's `%APPDATA%`, and the app reads that copy first. Polytropos does the same: it reads the port from the newest such copy, and from the plain file only without one, so the doctor, setup's default URL, the guard hook and the router's default upstream follow it. Setup writes the URL into `upstreams.freellmapi.base_url`, so after the app moved, run `hermes polytropos setup` again. The doctor and the guard hook follow the saved port by themselves, in the gateway after a restart, because each process reads the port once.

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

With `HOST` set to 127.0.0.1, FreeLLMAPI listens on this machine only.

FreeLLMAPI encrypts the provider keys in its database with `ENCRYPTION_KEY`. Polytropos writes it into `data/.env` once. It takes the value from `data/.encryption-key` when that file holds one, as after `adopt`, and otherwise makes a random key of 64 hex characters. A key that is already set is never replaced. Keep `data/.env` together with the database. The backups include both.

### Updates and backups

In managed mode, `hermes polytropos update freellmapi`, and `update` with `all`, run these steps.

1. Build the new tag in `releases/<new tag>/` while the running service stays up. A failed build changes nothing.
2. Stop the service and wait up to 30 seconds until port 3001 is free.
3. Copy the files in `data/` to `backups/<UTC time>-<old tag>-to-<new tag>/`, and delete all but the newest five backups.
4. Point `current` at the new release and start the service.
5. Check the new version. FreeLLMAPI has to answer `/v1/models` with JSON (status 200 or 401) within 60 seconds. Then stack-canary's tool checks run once each on the first cloud model in the routes whose upstream is on this machine.
6. When a check fails, stop the service, restore the backup, point `current` back at the old release and start it. The restore first deletes the database's `-wal` and `-shm` files, so stale ones cannot damage the restored database.

The update rolls back when FreeLLMAPI does not answer `/v1/models` within 60 seconds, or when the tested model does not pass all of A1 to A6. Every status other than `pass`, such as a stack-canary `error` from a rate limit, counts as not passing, and so does a check without a row in stack-canary's summary. An error while the new version starts or is checked also rolls the update back. When the tool-call check cannot run at all, update prints that it was skipped and why, and the new version stays. That happens when the router plugin cannot be loaded or no route has a cloud model on this machine, when stack-canary is not installed, in which case update names `hermes polytropos install`, and when stack-canary cannot run or leaves no summary that Polytropos can read. When the old version does not start again after a restore, Polytropos prints the command to start it by hand.

`hermes polytropos rollback freellmapi` runs step 6 by hand, with the newest backup.

## Programs and integrations

The pins are in `versions.lock`. Polytropos checks a program with `<program> --version`, rtk as `~/.local/bin/rtk --version` and stack-canary with `canary run --help`. Each command starts through the `PATH` lookup of Python's `shutil.which`, so on Windows npm's `.cmd` files run as well. A default program is installed when the check shows another version or nothing. Ollama only has to be present.

Every third-party program that Polytropos starts gets only part of the environment, so that it does not see the provider keys in Hermes's environment: npm, npx and uv, which run the scripts of the packages they install, git and node, the downloaded rtk, graphify and stack-canary, and the version checks of the pinned programs. They get `PATH`, `HOME`, `USER`, `LOGNAME`, `TERM`, `LANG`, `LANGUAGE`, `TMPDIR`, the `LC_*` variables, the proxy variables, the CA certificate variables `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `GIT_SSL_CAINFO`, the version manager variables `NVM_DIR`, `VOLTA_HOME`, `ASDF_*` and `MISE_*`, and the `npm_config_*`, `UV_*` and `XDG_*` variables. On Windows they also get `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP` and `TMP`, because node does not start without `SYSTEMROOT` and npm and uv find their folders through the others. The same holds for `npm ci` and `npm run build` when Polytropos builds FreeLLMAPI, and for `npm uninstall -g` and `uv tool uninstall` during uninstall. Three steps get one variable more: `rtk init --agent hermes` and its `--uninstall` get the profile's home as `HERMES_HOME`, FreeLLMAPI's `setup-opencode` gets `FREELLMAPI_API_KEY` and `FREELLMAPI_URL` (see OpenCode below), and `canary run` gets only the key variable that its upstream names in `api_key_env`, with the value Hermes's secret scope gives for this profile. The exceptions are `hermes` itself, which keeps Hermes's environment because it is the host; FreeLLMAPI's service, which gets the fixed environment under [Files](#files); the operating system's own programs `launchctl`, `systemctl`, `systemd-inhibit`, `caffeinate`, `pmset` and `sysctl`; and Hermes's own Python, which holds the sleep on Windows with a fixed script.

- **rtk.** Downloads the release archive for this platform (`darwin-arm64`, `darwin-x86_64`, `linux-x86_64`, `linux-aarch64` or `windows-x86_64`), checks its SHA-256, installs the binary as `~/.local/bin/rtk`, then runs `~/.local/bin/rtk init --agent hermes`, which adds rtk's Hermes plugin to the profile's `plugins/rtk-rewrite/`. That command and its `--uninstall` get the profile's home as `HERMES_HOME`, because rtk otherwise uses `~/.hermes`, also on Windows. Another rtk on the `PATH` does not count as installed. On Windows the archive is a zip, and its `rtk.exe` replaces `~/.local/bin/rtk.exe` in place. Windows cannot overwrite a running exe, so install first renames the old one to `rtk.exe.old`, which the next install deletes, and puts it back when the new one cannot take its place. That is the folder where uv puts its tools on Windows too. rtk has no build for Windows on arm64, so install stops there with the platform and machine error.
- **graphify.** `uv tool install graphifyy==<version>`, then `graphify install --platform hermes`, which writes graphify's skill to `~/.hermes/skills/graphify/`, or `%LOCALAPPDATA%\hermes\skills\graphify\` on Windows. graphify ignores `HERMES_HOME`, so the skill lands in Hermes's default home whatever the profile.
- **ast-grep.** `uv tool install ast-grep-cli==<version>`, then `hermes skills install official/software-development/ast-grep`.
- **stack-canary.** `uv tool install git+https://github.com/Lil-Clanky/stack-canary@<commit>`. stack-canary has no version flag, so `install` records the commit it installed in `state.json`. It counts as installed at its pin when `canary run --help` works and that record is the pinned commit. Without a record, or with a record of another commit, `install` and `update` install the pinned commit.
- **OpenCode**, optional. `npm install -g opencode-ai@<version>`, then `npx -y freellmapi@<cli_version> setup-opencode`, then `hermes skills install official/autonomous-ai-agents/opencode`. `setup-opencode` also gets the profile's `FREELLMAPI_API_KEY`, FreeLLMAPI's own key, and asks for it when the profile has none. It gets `FREELLMAPI_URL` as well, `http://127.0.0.1:3001`, or the desktop app's port on Windows, because the CLI looks for FreeLLMAPI on `localhost:3001` without it.
- **qmd**, optional. `npm install -g @tobilu/qmd@<version>`, then `hermes skills install official/research/qmd`. qmd downloads and runs local GGUF models of its own, which is heavy on a machine with little memory.
- **context7**, optional. `npm install -g @upstash/context7-mcp@<version>`, then `hermes mcp add context7 --command context7-mcp`. When the profile has a `context7` entry already, install leaves it as it is and skips that step. When `hermes mcp add` fails, install says that the MCP server was not added, prints the command to run yourself and goes on.
- **Ollama**, optional and only when missing. On macOS install downloads `ollama-darwin.tgz` of the pinned version, checks its SHA-256, unpacks it into `~/.local/lib/polytropos/ollama/<version>/` and links `~/.local/bin/ollama` to it. On Linux Polytropos does not install Ollama, because Ollama's install script asks for `sudo`. Install prints `Polytropos does not install Ollama on Linux. Install it with: curl -fsSL https://ollama.com/install.sh | sh` and counts Ollama as not installed. On Windows it prints `Polytropos does not install Ollama on Windows. Install it in PowerShell with: irm https://ollama.com/install.ps1 | iex`, the command from Ollama's download page, because that script has no checksum to pin either. On macOS Polytropos does not start Ollama, so run `ollama serve` yourself.

## Canary data

### The published matrix

The router and the doctor read stack-canary's published results from `https://raw.githubusercontent.com/Lil-Clanky/stack-canary/matrix/matrix.json`. Polytropos keeps a copy in `~/.cache/polytropos/canary-matrix.json` and downloads a new one when the copy is older than 86400 seconds, or when its modification time lies in the future. When the download fails, it uses the old copy, or an empty matrix when there is none, and writes the stamp file `canary-matrix.json.failed` next to the copy. While that stamp is younger than 3600 seconds, Polytropos tries no new download and uses the old copy. A good download removes the stamp.

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
| `generated` | The time of the newest run in the matrix. Polytropos does not read it. |
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
| `~/.hermes/.env` | `FREELLMAPI_API_KEY`, written by `hermes config set FREELLMAPI_API_KEY <key>` |
| `~/.hermes/plugins/polytropos/`, `polytropos-router/`, `polymetis/`, `superpowers/`, `ponytail/` | The pack plugins |
| `~/.hermes/plugins/polytropos/versions.lock` | The copy of `versions.lock` that the commands read |
| `~/.hermes/plugins/.install-metadata.json` | Hermes's install records, which the doctor reads |
| `~/.hermes/plugins/whatsapp-human-pace/` | whatsapp-human-pace with its `settings.json` and `pending.json`, when installed |
| `~/.hermes/plugin-data/agent-plugin-polymetis-1717164d/` | Polymetis's `decisions.jsonl` and `index-<platform>.json`, mode 0600, when installed and `log` is on |
| `~/.hermes/skills/graphify/` | graphify's Hermes skill, written by graphify to Hermes's default home |
| `~/.hermes/plugins/rtk-rewrite/` | rtk's Hermes plugin, written by `rtk init --agent hermes` |
| `~/.config/opencode/opencode.json` | OpenCode's config, where FreeLLMAPI's `setup-opencode` adds its provider and sets a default model |
| `~/.local/share/opencode/auth.json` | OpenCode's stored keys, where FreeLLMAPI's `setup-opencode` stores its key under `freellmapi` |
| `~/.local/share/polytropos/freellmapi/` | Managed FreeLLMAPI, see [Files](#files) |
| `~/.local/share/polytropos/canary/openai-results.json` | Results of `canary run` |
| `~/.local/state/polytropos/state.json` | What Polytropos installed and changed, mode 0600 |
| `~/.local/state/polytropos/freellmapi.log` | FreeLLMAPI's output on macOS |
| `~/.local/state/polytropos/router-traces.jsonl` | Router traces when `save_traces` is on, mode 0600 |
| `~/.cache/polytropos/canary-matrix.json` | Cached stack-canary matrix |
| `~/.cache/polytropos/canary-matrix.json.failed` | Stamp of a failed matrix download, see [The published matrix](#the-published-matrix) |
| `~/Library/LaunchAgents/io.github.lil-clanky.polytropos.freellmapi.plist` | FreeLLMAPI service on macOS |
| `~/.config/systemd/user/polytropos-freellmapi.service` | FreeLLMAPI service on Linux |
| `~/.local/bin/rtk` | rtk, `rtk.exe` on Windows |
| `~/.local/bin/ollama`, `~/.local/lib/polytropos/ollama/<version>/` | Ollama on macOS |

On Windows the Hermes paths are under `%LOCALAPPDATA%\hermes` instead of `~/.hermes`, and the `polytropos` folders under `~/.local/share`, `~/.local/state` and `~/.cache` are one folder, `%LOCALAPPDATA%\polytropos`. Windows keeps no POSIX modes, so the files marked mode 0600 are as private as their folder in the user's profile.

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

`profiles` is keyed by the profile's Hermes home. `config_changes` lists each changed key with its first old value, `null` when the key did not exist, and its latest new value. Setup records each `model` key it changes on its own and never records `model.api_key`. The setup of Polytropos 0.1.1 recorded the whole `model` section under the key `model`, API key included, as it was before FreeLLMAPI's `setup-hermes` rewrote it. When Polytropos loads such a `state.json`, it turns that record into one record per key of the old and the new section and saves the file at once. The record for `model.api_key` holds no key. It holds only the `${...}` pointer that the setup wrote, `null` otherwise, and `old_dropped`, which says whether the old section had a key. See [uninstall](#uninstall). An MCP server that `install` added is recorded here too, under `mcp_servers.<name>`, and an entry that was there before is not recorded. `integrations` lists the other set-up commands that `install` ran for this profile, each with the folder or file it added or changed. `shared` has an entry per shared part, `freellmapi` or a program name, with the profiles that use it, whether Polytropos installed it and its version. The FreeLLMAPI entry also has the mode. For stack-canary the version is the short commit that `install` installed, which the doctor and `install` compare with the pin.

## Hermes v0.23.2 notes

- **Pack install.** `hermes plugins pack install` needs a terminal and one `y` at Hermes's review screen. It exits with status 1 when any entry fails. Hermes reads ponytail's `plugin.json` as an Agent Plugins manifest and refuses it, because the file has no `$schema`. That holds for v0.23.2 and for Hermes's main branch of 2026-11-23. The pack install prints `Plugin 'ponytail' is unavailable: plugin.json declares an unsupported or missing Agent Plugins schema.` and exits with status 1, also when the other plugins installed. `hermes polytropos install` installs ponytail with `hermes plugins install`, which runs the same check, so Hermes refuses ponytail there too. Polytropos says so and does not count it as a failure. ponytail stays missing until Hermes accepts its `plugin.json`.
- **Install scan.** Hermes scans each plugin before it installs it, after the review screen, which shows no verdict. A `safe` verdict installs the plugin, and a `dangerous` verdict always stops it. A `caution` verdict stops a plugin in a pack install with `Security scan blocked plugin install:` and the findings. `hermes plugins install` of one plugin shows the findings and asks first. With `--force` Hermes accepts a caution verdict without printing the findings and only notes it in its log. The scan gives polytropos a caution verdict, with findings about launch agents, systemd services, git clones and `sudo`. polytropos writes a LaunchAgent or a systemd unit for FreeLLMAPI, clones the git repository of FreeLLMAPI, and its doctor names `sudo pmset -a sleep 0` as a fix for you to run. The first pack install therefore stops polytropos and prints its findings. Read them, then run the same command again with `--force`. This was read from Hermes's source and its scanner and has not been tried in a real install.
- **Reinstall.** Installing the pack over an earlier install needs `--force`. Without it each plugin that is installed already fails with a message that it already exists, except polytropos, which the scan stops first. With `--force` Hermes also accepts a caution verdict without printing the findings, and a dangerous verdict still stops the plugin. `hermes plugins pack install` without `--force`, or `hermes plugins install` of one plugin, shows the findings.
- **Shallow fetch.** Hermes fetches a plugin with `git clone --depth 1` and `git fetch --depth 1 origin <commit>`, and that fetch fails now and then with `fatal: shallow file has changed since we read it`. Hermes does not retry it, so run the install again with `--force`.
- **Plugin folders.** A pack entry with a `subdir` installs under the plugin's manifest name, so `plugins/polytropos_router` becomes `~/.hermes/plugins/polytropos-router/`.
- **Pack config seeds.** A seed lands at the top level of `plugins.entries.<plugin>` and only fills keys that are absent. Hermes refuses seed keys whose names match `(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key|credential|auth)`, and that pattern catches `max_tokens` and `api_key_env`.
- **Router key.** Hermes refuses a turn on the router when `FREELLMAPI_API_KEY` has no value for the profile, also when every route is local. The error starts with `No usable credentials found for provider 'polytropos-router'. Set FREELLMAPI_API_KEY.` When fallback providers are set with `hermes fallback`, the gateway and the TUI walk that chain instead, so the turn runs without the router. `hermes config set FREELLMAPI_API_KEY <key>` stores the key in the profile's `.env`.
- **Context minimum.** Hermes refuses a model that reports a context window below 64,000 tokens. The router reports at least 64,000.
- **Provider switch.** `hermes config set model.provider` removes `model.base_url` and `model.api_mode` when they belonged to the previous provider. Polytropos records both before it switches the provider, so uninstall can restore them.
- **Model section.** `hermes config set model '<mapping>'` is refused while `model` is a mapping, and a plain string goes into `model.default`. With `--force` Hermes replaces the whole section. Polytropos never sets the whole section. Setup and uninstall change one `model` key at a time.
- **Output limit.** Hermes has no setting for the output limit, so a normal turn reaches the router without `max_tokens`. Hermes sets a one-time `max_tokens` when it retries a request. See [Window fit](#window-fit).
- **Asynchronous auxiliary calls.** An asynchronous auxiliary call, such as vision analysis, runs one full routed request on the router and then fails, and Hermes moves on to its next provider. `auxiliary.vision.provider` set to another provider avoids it. See [Hermes's asynchronous calls](#hermess-asynchronous-calls).
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
3. Commit the pack file, tag this second commit and publish a GitHub release for the tag.
4. For the Hermes plugin catalog, open a pull request on `NousResearch/hermes-agent` that sets `sha` and `version` in `plugin-catalog/polytropos.yaml` and in `plugin-catalog/polytropos-router.yaml` to the code commit from step 1. A maintainer reviews the commits between the old and the new pin before the catalog moves.

The pack pins Polymetis to a commit of its own repository, `Lil-Clanky/polymetis`. Move that pin with `python scripts/release.py --repo Lil-Clanky/polymetis --sha <commit>` before step 3.

### Pins

`versions.lock` pins FreeLLMAPI (tag, commit, Node range and CLI version), the programs and the third-party plugins. A comment above each section says where its values came from and gives the license. `python scripts/check_updates.py` compares the pins with the newest upstream releases and prints the updates as JSON, and `--lock PATH` reads another lock file. `--apply <component>` writes one update into `versions.lock`. For ponytail and superpowers it also sets their `ref` in `hermes-pack.yaml`. The components are `freellmapi`, `freellmapi-cli`, `rtk`, `graphify`, `ast-grep`, `ponytail` and `superpowers`. The pins of stack-canary, Ollama and the optional programs are moved by hand.

After a change to `versions.lock`, run `python scripts/sync_shared.py`, which copies the file into `plugins/polytropos/`. A pack install copies only the plugin folders, so the commands read that copy.

### The weekly bump job

`.github/workflows/bump.yml` runs every Monday at 06:17 UTC, and you can start it by hand (`workflow_dispatch`). It asks `check_updates.py` which components have an update and starts one job per component. Each job applies its update, runs `sync_shared.py` and the tests without the `e2e` ones, and pushes the branch `bump/<component>`. Then it opens a pull request for that branch, or updates the open one, so there is one pull request per component. For FreeLLMAPI the job also builds the new tag, starts it with an empty database, and checks that `/livez` answers and that `/v1/models` answers 200 or 401 with JSON. Because the database starts empty, CI does not check a database migration. On a user's machine the update backs up the database first and rolls back when the new version does not answer, and `hermes polytropos rollback freellmapi` restores the backup by hand.

The job needs the repository setting "Allow GitHub Actions to create and approve pull requests", under Settings, Actions, General, Workflow permissions. Without it the job cannot open the pull request.

### Shared code

`shared/polytropos_shared/` holds the code that both plugins use, such as the paths, the memory guard and the canary reader. `python scripts/sync_shared.py` copies it into `plugins/polytropos/_shared/` and `plugins/polytropos_router/_shared/`, and `--check` exits with status 1 when a copy differs. The copies are committed, because Hermes installs a plugin folder straight from git. The test run syncs them at the start, and CI runs `--check` before the tests.

### Tests

CI (`.github/workflows/test.yml`) runs on `ubuntu-latest` and `macos-latest` with Python 3.11 and 3.13. It installs `openai>=2`, `pytest` and `pyyaml`, runs `python scripts/sync_shared.py --check` and then `python -m pytest -q -m "not e2e"`. The router tests run against a fake OpenAI-compatible server on loopback, `tests/fake_openai.py`, and the `tmp_home` fixture points `HOME`, `HERMES_HOME` and the XDG folders at a temporary folder. The end-to-end tests in `tests/e2e` are marked `e2e` and run only on your own machine with `POLYTROPOS_E2E=1`. `python scripts/hermes_sandbox.py build-root .sandbox/e2e` builds their root, and `scripts/hermes_sandbox.py` makes the isolated Hermes homes inside it. The README's development section says how to run them.
