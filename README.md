# Polytropos

Polytropos is a plugin pack for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It sets up an existing Hermes install to use free cloud models through [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) and small local models through [Ollama](https://ollama.com), on macOS and Linux.

The pack has two plugins of its own.

- `polytropos-router` is a model provider. A session runs on its model `auto`, and the router chooses a real model for each turn. A short classifier call puts the turn on the `simple` or the `complex` route, and each route is a list of models that the router tries in order. A local model only gets the turn when the machine has memory to spare.
- `polytropos` adds the `hermes polytropos` command. It installs the other parts at pinned versions, runs FreeLLMAPI as a background service and checks that everything works together. It also pauses turns on a local model when memory runs low, and keeps the machine awake while a turn runs.

Every command, setting and path is listed in [docs/reference.md](docs/reference.md).

## What gets installed

| Part | What it does | Installed by |
|---|---|---|
| polytropos, polytropos-router | The two plugins above | The pack |
| superpowers | Hermes plugin with a set of working skills | The pack |
| ponytail | Hermes plugin for shorter replies and smaller code changes | `hermes polytropos install` |
| FreeLLMAPI | Local server that pools the free tiers of many model providers behind one OpenAI-compatible API | `hermes polytropos install` |
| rtk | Compresses command output to save tokens | `hermes polytropos install` |
| graphify | Builds a graph of a codebase or a folder of documents, with a Hermes skill | `hermes polytropos install` |
| ast-grep | Structural code search, with the official Hermes skill | `hermes polytropos install` |
| stack-canary | Tests whether a model's tool calls work | `hermes polytropos install` |

`hermes polytropos install` also offers these, with one question each.

| Part | What it does |
|---|---|
| OpenCode | Coding agent for the terminal, set up to use FreeLLMAPI, with the opencode skill |
| qmd | Local search over notes and documents, with the qmd skill. It runs local models of its own, which takes memory. |
| context7 | MCP server with library documentation |
| Ollama | Local model runtime, installed only when it is missing |

The programs are pinned in [versions.lock](versions.lock) and the plugins in [hermes-pack.yaml](hermes-pack.yaml), each at a fixed version or commit.

## Requirements

- Hermes Agent v0.23.2, installed and working. The doctor warns when it finds another version.
- macOS, or Linux with systemd. The FreeLLMAPI service runs as a LaunchAgent on macOS and as a systemd user unit on Linux.
- git and [uv](https://docs.astral.sh/uv/).
- Node.js `>=20.18.0 <25.0.0` with npm, to build and run FreeLLMAPI. Polytropos does not install Node, and the doctor checks its version.
- `~/.local/bin` on your `PATH`. Polytropos puts rtk there, and on macOS also Ollama.

## Install

Run these commands in a terminal, in the Hermes profile you want to set up.

```sh
git clone https://github.com/Lil-Clanky/polytropos
hermes plugins pack install polytropos/hermes-pack.yaml
hermes plugins pack install --force polytropos/hermes-pack.yaml
hermes polytropos doctor
hermes polytropos install
hermes polytropos setup
hermes polytropos doctor
```

Here is what each step does.

1. **Get the pack file.** Clone the repository, or download `hermes-pack.yaml` from a release and install that file.
2. **Install the pack.** Hermes shows its review screen, a list of the plugins with their sources and pinned commits, and asks you to confirm with `y`. That question is why the pack install needs a terminal. Then Hermes scans each plugin before it installs it. On Hermes v0.23.2 the first run stops polytropos and prints the findings of its scan, prints a failure line for ponytail, and exits with status 1. The scan gives polytropos a caution verdict, with findings about launch agents, systemd services, git clones and `sudo`. They come from what the plugin does. It writes a LaunchAgent or a systemd unit to run FreeLLMAPI, it clones the git repositories of FreeLLMAPI and ponytail, and its doctor names `sudo pmset -a sleep 0` as a fix for you to run. Read the findings for polytropos, and for any other plugin that the scan stopped, then run the same command again with `--force`. With `--force` Hermes installs a plugin with a caution verdict without printing its findings, and a dangerous verdict still stops the plugin. The second run installs polytropos, polytropos-router and superpowers and prints the ponytail failure again. Step 4 installs ponytail another way. When an entry fails with `fatal: shallow file has changed since we read it`, run the command with `--force` once more. What this step says about the scan was read from Hermes's source and its scanner and has not been tried in a real install.
3. **Run the doctor.** On a new install it reports FreeLLMAPI, ponytail and the programs as missing.
4. **Install the rest.** `hermes polytropos install` builds FreeLLMAPI at its pinned tag and runs it as a service on `127.0.0.1:3001`. It asks before it installs ponytail from its pinned commit, installs the default programs, and asks about each optional one. When a FreeLLMAPI already answers on port 3001, Polytropos uses that one as it is and leaves its updates to you. To let Polytropos run it instead, stop it and run `hermes polytropos adopt --from <its checkout>`. When another program holds port 3001, install stops and asks you to free the port first.
5. **Add provider keys.** Open FreeLLMAPI's dashboard at http://127.0.0.1:3001, add your provider keys, and copy FreeLLMAPI's unified API key, which the next step asks for.
6. **Connect Hermes.** `hermes polytropos setup` runs FreeLLMAPI's own `setup-hermes`. When it asks for an API key, paste the unified key. `setup-hermes` stores the key as `FREELLMAPI_API_KEY` in the profile's `.env` and rewrites the profile's `model` section so that FreeLLMAPI's model is the default. Polytropos keeps a copy of the `model` section from before, so that uninstall can put it back. Then it asks whether to make the router the default model instead, and writes the router settings.
7. **Run the doctor again.** When no check fails and the router is not the default model yet, the doctor asks whether to make it the default.

### The FreeLLMAPI key

Hermes needs `FREELLMAPI_API_KEY` for every turn on the router, also when all routes are local. Without it Hermes stops the turn with an error that starts with `No usable credentials found for provider 'polytropos-router'. Set FREELLMAPI_API_KEY.` When fallback providers are set with `hermes fallback`, the gateway and the TUI run the turn on the first fallback that has credentials instead, without the router. `hermes polytropos setup` leaves the key in the profile's `.env`.

### Restarting the gateway

A running gateway keeps the old plugin code until it restarts. Hermes prints `hermes gateway restart` after a pack install, and `hermes polytropos update` and `uninstall` print it when they change plugins. Polytropos never restarts the gateway itself, because a restart interrupts running chats.

### Profiles

`hermes polytropos` works on the active Hermes profile. Plugins, settings and the FreeLLMAPI key belong to a profile, so run the install steps in each profile that should use Polytropos, for example `hermes --profile <name> polytropos setup`. FreeLLMAPI and the programs are shared by the whole machine. Polytropos records which profiles use each shared part, and an uninstall from one profile removes a shared part only when no other profile uses it. In a second profile, `hermes polytropos install` adds the Hermes skills, rtk's Hermes plugin and the MCP entries that this profile lacks, and records them for this profile, so that its uninstall removes only those.

## What the doctor checks

`hermes polytropos doctor` prints one line per finding, marked `ok`, `warn`, `fail` or `info`, with a fix on the next line where it has one. It exits with status 1 when a check fails. It looks at the following.

- The Hermes version.
- The pack plugins, against the commits pinned in `versions.lock` and the commit of the installed polytropos plugin.
- Whether FreeLLMAPI answers on 127.0.0.1:3001, whether it accepts the profile's `FREELLMAPI_API_KEY`, and in which mode Polytropos runs it.
- Whether the Node version fits FreeLLMAPI, unless FreeLLMAPI runs in external mode.
- The router settings, cloud models with a window below Hermes's minimum of 64,000 tokens, and upstreams that do not answer.
- A leftover `router:` section from Hermes's closed router pull request, which Polytropos ignores.
- The pinned programs and their versions.
- stack-canary results for the models in the routes.
- Whether the local models fit a machine with 16 GB of RAM or less.
- Files in `~/Downloads`, `~/Desktop` and the home folder whose names look like key exports. It reads names and permissions only.
- The other Hermes profiles, and whether Polytropos is installed in them.
- ponytail's token cost in gateway chats, and a note that the superpowers bootstrap after a context compaction is not verified on Hermes v0.23.2.
- On macOS with a Hermes gateway LaunchAgent, whether the Mac goes to sleep. The doctor shows the command to change that and leaves it to you.

## Updates

```sh
hermes polytropos update
```

`update` reads the latest Polytropos release from GitHub, shows what would change and asks before it changes anything. It updates the plugins with `hermes plugins pack install --force` and the release's pack file, so Hermes asks for its `y` again. With `--force` Hermes also installs a plugin with a caution verdict from its scan without printing the findings, and a dangerous verdict still stops the plugin. To see the findings, run `hermes plugins pack install` without `--force` on the release's `hermes-pack.yaml`, or `hermes plugins install` for one plugin. Then `update` brings ponytail and the programs to the versions of that release, and FreeLLMAPI as well when Polytropos runs it. `hermes polytropos update programs` and `hermes polytropos update freellmapi` update one part to the versions that the installed polytropos plugin pins, and fetch nothing from GitHub.

Before a FreeLLMAPI update, Polytropos prints the installed tag, the new tag and a GitHub link that compares the two. It builds the new version next to the running one, stops the service, backs up the database and keys, starts the new version and checks that it answers. Then stack-canary runs its tool checks once on a cloud model from the router's routes. Polytropos puts the backup and the old version back when the new version does not answer within 60 seconds, or when the tested model does not pass all six checks, A1 to A6. A stack-canary `error`, such as a rate limit, counts as not passing. When the tool-call check cannot run at all, for example because stack-canary is not installed or no route has a cloud model on this machine, update says why and keeps the new version. To go back by hand later, run `hermes polytropos rollback freellmapi`. It asks first, because a rollback loses the changes made in FreeLLMAPI since the update.

Polytropos does not update anything until you run `update`.

## Uninstall

```sh
hermes polytropos uninstall
```

Uninstall sets the settings that Polytropos changed in this profile back to their old values. That includes the `model` section that FreeLLMAPI's `setup-hermes` rewrote, so the model you used before the setup comes back. It also undoes the integrations that `install` recorded for this profile, such as the Hermes skills and the context7 MCP entry. It removes the FreeLLMAPI service and the programs that Polytropos installed, once no other profile uses them. At the end it prints a `hermes plugins remove` command for each pack plugin. Run those yourself, then `hermes gateway restart`. When a restore, an undo or a removal fails, uninstall exits with status 1 and keeps its record, so the next uninstall tries again.

Some things stay.

- FreeLLMAPI's database, the provider keys and the update backups, in `~/.local/share/polytropos/freellmapi`. `hermes polytropos uninstall --purge-data` deletes that folder after you type `yes`.
- Programs that were on the machine before Polytropos, and the skills and MCP entries that the profile had before the install.
- Shared parts that another profile still uses.
- A FreeLLMAPI that Polytropos did not install. Polytropos leaves it alone.
- The `FREELLMAPI_API_KEY` line that FreeLLMAPI's `setup-hermes` wrote to the profile's `.env`.
- The changes that FreeLLMAPI's `setup-opencode` made for OpenCode. It added the `freellmapi` provider and a default model to `~/.config/opencode/opencode.json`, and stored FreeLLMAPI's key under `freellmapi` in `~/.local/share/opencode/auth.json`. Uninstall prints both paths, and you change them by hand.
- Polytropos's own files in `~/.local/state/polytropos`, `~/.cache/polytropos` and `~/.local/share/polytropos/canary`.

## What stays on this machine

- In managed mode FreeLLMAPI listens on 127.0.0.1 only.
- Provider keys stay in FreeLLMAPI's encrypted database, and the unified key in the profile's `.env`. Polytropos does not print or log key values. When your `model` section held a key before the setup, `~/.local/state/polytropos/state.json` keeps a copy of that section, with mode 0600, until uninstall puts it back.
- The router writes no message text to disk. Its traces are off by default. When you turn them on, they record the route and the model of each request without message text, in a file that only your user can read.
- The classifier sends the last user message of each turn to the classifier model, which is FreeLLMAPI's `auto` model by default. With a cloud classifier that text reaches a cloud provider, also when the turn itself then runs on a local model.
- `hermes polytropos canary run` only tests endpoints on this machine, and its results stay in `~/.local/share/polytropos/canary`.
- The doctor's key-file check reads file names and permissions and does not open the files.
- Programs are installed at the versions in `versions.lock`. The FreeLLMAPI checkout is checked against its pinned commit, and the rtk and macOS Ollama archives against their SHA-256.
- Outside installs and updates, Polytropos contacts GitHub only for stack-canary's public results, at most once a day. `update` without a part reads the latest release from GitHub. Installs and updates download from GitHub, npm and PyPI, and for Ollama on Linux from ollama.com.

## WhatsApp pacing

The repository also holds `whatsapp-human-pace`, a plugin for Hermes's WhatsApp gateway. It holds incoming WhatsApp messages, answers after a random delay of 5 to 10 minutes, and batches what arrived in the meantime into one turn. It also turns Markdown in WhatsApp replies into plain text and removes most emoji. By default the first reply in a chat each day starts with the note "Replies in this chat come from an assistant." The plugin is not part of the pack. Install it on its own with

```sh
hermes plugins install Lil-Clanky/polytropos/plugins/whatsapp_human_pace
```

Hermes asks whether to enable it, and the default answer is no. From the terminal or any chat other than WhatsApp, `/pace` shows or changes the delay. The settings are in the reference.

## Development

The tests use pytest, `openai` and `pyyaml`.

```sh
pip install "openai>=2" pytest pyyaml
python scripts/sync_shared.py --check
python -m pytest -q -m "not e2e"
```

CI runs these on Ubuntu and macOS with Python 3.11 and 3.13. Code that both plugins use lives in `shared/polytropos_shared/`. `scripts/sync_shared.py` copies it into each plugin's `_shared/` folder and copies `versions.lock` into the polytropos plugin, and `--check` exits with status 1 when a copy is out of date. The copies are committed, because Hermes installs a plugin folder straight from git. A test run refreshes them as well.

### End-to-end tests

The end-to-end tests in `tests/e2e` run the Hermes Agent from `~/.hermes/hermes-agent` in an isolated home under `.sandbox/e2e`, against fake OpenAI-compatible servers on loopback ports. They run on your own machine only, and CI leaves them out. Build their root once.

```sh
python scripts/hermes_sandbox.py build-root .sandbox/e2e
```

`build-root` runs one turn against a fake server. That first run in a new root prepares Hermes's own runtime there, which takes about 850 MB. Hermes installs its Python packages there with uv and fetches its plugin catalog and the models.dev data. Then run the tests.

```sh
POLYTROPOS_E2E=1 python -m pytest tests/e2e -q -rs
```

`-rs` prints why each skipped test was skipped. Without `POLYTROPOS_E2E=1` every test is skipped. A test also skips when the root is missing, when memory pressure is at warning or critical, or when less than 5 GiB of disk is free where the root lives. The launchd test runs on macOS only. On macOS each Hermes run starts a one-off launchd job for the sandbox gateway, and Hermes removes finished ones the next time it starts a gateway.

How a release is made and how the weekly update job works is described in the maintainer section of the reference.

## License

MIT, see [LICENSE](LICENSE). Polytropos installs the other parts from their own sources under their own licenses, which `versions.lock` lists.
