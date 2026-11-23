# Polytropos

![Odysseus turns a switch between a local chip and the cloud](docs/images/banner.webp)

Polytropos is a plugin pack for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It sets up an existing Hermes install to use free cloud models through [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) and small local models through [Ollama](https://ollama.com), on macOS and Linux.

The pack has two plugins of its own.

- `polytropos-router` is a model provider. A session runs on its model `auto`, and the router chooses a real model for each turn. A short classifier call puts the turn on the `simple` or the `complex` route, and each route is a list of models that the router tries in order. A local model only gets the turn when the machine has memory to spare and the request fits the `context_window` set for it.
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
| Ollama | Local model runtime, installed on macOS only when it is missing. On Linux install prints Ollama's own install command for you to run. |

The programs are pinned in [versions.lock](versions.lock) and the plugins in [hermes-pack.yaml](hermes-pack.yaml), each at a fixed version or commit.

## Requirements

- Hermes Agent v0.23.2, installed and working. The doctor warns when it finds another version.
- macOS, or Linux with systemd. The FreeLLMAPI service runs as a LaunchAgent on macOS and as a systemd user unit on Linux.
- A Mac on arm64 or x86_64, or a Linux machine on x86_64 or aarch64. rtk has release downloads for these machines only. On another machine `hermes polytropos install` stops with an error that names the platform and the machine, and `update` does the same when it reaches the programs.
- git and [uv](https://docs.astral.sh/uv/).
- Node.js `>=20.18.0 <25.0.0` with npm, to build and run FreeLLMAPI. Polytropos does not install Node, and the doctor checks its version.
- `~/.local/bin` on your `PATH`. Polytropos puts rtk there, and on macOS also Ollama.

## Install

Run these commands in a terminal, in the Hermes profile you want to set up.

```sh
git clone https://github.com/Lil-Clanky/polytropos
hermes plugins pack install polytropos/hermes-pack.yaml
```

This first pack install stops polytropos and prints the findings of Hermes's install scan, as step 2 below explains. Read them, then run the pack install again with `--force`.

```sh
hermes plugins pack install --force polytropos/hermes-pack.yaml
```

Then run the doctor, which reports what is still missing.

```sh
hermes polytropos doctor
```

Install the rest. Install asks before it installs ponytail and before each optional program.

```sh
hermes polytropos install
```

Add your provider keys in FreeLLMAPI's dashboard and copy its unified API key, as step 5 below explains. Store that key in the profile, then run setup.

```sh
hermes config set FREELLMAPI_API_KEY <unified key>
hermes polytropos setup
```

Run the doctor again. It can ask whether to make the router the default model.

```sh
hermes polytropos doctor
```

Here is what each step does.

1. **Get the pack file.** Clone the repository, or download `hermes-pack.yaml` at a release tag from `https://raw.githubusercontent.com/Lil-Clanky/polytropos/<tag>/hermes-pack.yaml` and install that file. Releases do not attach the file.
2. **Install the pack.** Hermes shows its review screen, a list of the plugins with their sources and pinned commits, and asks you to confirm with `y`. That question is why the pack install needs a terminal. Then Hermes scans each plugin before it installs it. On Hermes v0.23.2 the first run stops polytropos and prints the findings of its scan, prints a failure line for ponytail, and exits with status 1. The scan gives polytropos a caution verdict, with findings about launch agents, systemd services, git clones and `sudo`. They come from what the plugin does. It writes a LaunchAgent or a systemd unit to run FreeLLMAPI, it clones the git repository of FreeLLMAPI, and its doctor names `sudo pmset -a sleep 0` as a fix for you to run. Read the findings for polytropos, and for any other plugin that the scan stopped, then run the same command again with `--force`. With `--force` Hermes installs a plugin with a caution verdict without printing its findings, and a dangerous verdict still stops the plugin. The second run installs polytropos, polytropos-router and superpowers and prints the ponytail failure again. Step 4 tries to install ponytail with `hermes plugins install`, which Hermes refuses for the same reason, also after v0.23.2, see [Hermes v0.23.2 notes](docs/reference.md#hermes-v0232-notes). When an entry fails with `fatal: shallow file has changed since we read it`, run the command with `--force` once more. What this step says about the scan was read from Hermes's source and its scanner and has not been tried in a real install.
3. **Run the doctor.** On a new install it reports FreeLLMAPI, ponytail and the programs as missing.
4. **Install the rest.** `hermes polytropos install` builds FreeLLMAPI at its pinned tag and runs it as a service on `127.0.0.1:3001`. It asks before it installs a missing ponytail from its pinned commit with `hermes plugins install`, leaves a ponytail that is there as it is, installs the default programs, and asks about each optional one. When a FreeLLMAPI already answers on port 3001, Polytropos uses that one as it is and leaves its updates to you. To let Polytropos run it instead, stop it and run `hermes polytropos adopt --from <its checkout>`. When another program holds port 3001, install stops and asks you to free the port first.
5. **Add provider keys.** Open FreeLLMAPI's dashboard at http://127.0.0.1:3001, add your provider keys, and copy FreeLLMAPI's unified API key, which the next step stores.
6. **Connect Hermes.** `hermes config set FREELLMAPI_API_KEY <unified key>` stores the key in the profile's `.env`. Then `hermes polytropos setup` asks whether to make the router the default model, and writes the router settings with `hermes config set`. Setup stops when the profile has no `FREELLMAPI_API_KEY`. It changes the `model` section only after a yes, one key at a time, and records the old values so that uninstall can put them back.
7. **Run the doctor again.** When no check fails, the profile has `FREELLMAPI_API_KEY` and the router is not the default model yet, the doctor asks whether to make it the default. Without the key it prints the `hermes config set FREELLMAPI_API_KEY` command instead.

### From the plugin catalog

When Polytropos is listed in the Hermes plugin catalog, you can install its two plugins by name instead of with the pack.

```sh
hermes plugins install polytropos
hermes plugins install polytropos-router
```

Hermes installs each one at the commit the catalog pins and does not stop at a caution verdict of its scan, because a maintainer read the code at that commit. superpowers comes with the pack only. Then continue with `hermes polytropos doctor` as above.

### The FreeLLMAPI key

Hermes needs `FREELLMAPI_API_KEY` for every turn on the router, also when all routes are local. Without it Hermes stops the turn with an error that starts with `No usable credentials found for provider 'polytropos-router'. Set FREELLMAPI_API_KEY.` When fallback providers are set with `hermes fallback`, the gateway and the TUI run the turn on the first fallback that has credentials instead, without the router. `hermes config set FREELLMAPI_API_KEY <key>` stores the key in the profile's `.env`.

### Restarting the gateway

A running gateway keeps the old plugin code until it restarts. Hermes prints `hermes gateway restart` after a pack install, and `hermes polytropos uninstall` prints it when it changes plugins. Polytropos never restarts the gateway itself, because a restart interrupts running chats.

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

`update` brings the programs and FreeLLMAPI, when Polytropos runs it, to the versions that the installed polytropos plugin pins in its `versions.lock`. It installs a missing ponytail after a yes, and for a ponytail at another commit it prints the command that moves it. It asks before it changes anything. `hermes polytropos update programs` and `hermes polytropos update freellmapi` update one part.

`update` does not change the Polytropos plugins themselves. Hermes updates those, so each new plugin commit goes through Hermes's own install and scan. After a catalog install, `hermes plugins update` moves the plugins to the commit the catalog pins. After a pack install, download `hermes-pack.yaml` of the newer release, or pull your clone, and run `hermes plugins pack install --force` on it. Then run `hermes polytropos update` to bring the other parts to the versions of that release.

Before a FreeLLMAPI update, Polytropos prints the installed tag, the new tag and a GitHub link that compares the two. It builds the new version next to the running one, stops the service, backs up the database and keys, starts the new version and checks that it answers. Then stack-canary runs its tool checks once on a cloud model from the router's routes. Polytropos puts the backup and the old version back when the new version does not answer within 60 seconds, or when the tested model does not pass all six checks, A1 to A6. A stack-canary `error`, such as a rate limit, counts as not passing. When the tool-call check cannot run at all, for example because stack-canary is not installed or no route has a cloud model on this machine, update says why and keeps the new version. To go back by hand later, run `hermes polytropos rollback freellmapi`. It asks first, because a rollback loses the changes made in FreeLLMAPI since the update.

Polytropos does not update anything until you run `update`.

## Uninstall

```sh
hermes polytropos uninstall
```

Uninstall sets the settings that Polytropos changed in this profile back to their old values. When you made the router the default model, the model you used before comes back. It also undoes the integrations that `install` recorded for this profile, such as the Hermes skills and the context7 MCP entry. It removes the FreeLLMAPI service and the programs that Polytropos installed, once no other profile uses them. At the end it prints a `hermes plugins remove` command for each pack plugin. Run those yourself, then `hermes gateway restart`. When a restore, an undo or a removal fails, uninstall exits with status 1 and keeps its record, so the next uninstall tries again.

Some things stay.

- FreeLLMAPI's database, the provider keys and the update backups, in `~/.local/share/polytropos/freellmapi`. `hermes polytropos uninstall --purge-data` deletes that folder after you type `yes`.
- Programs that were on the machine before Polytropos, and the skills and MCP entries that the profile had before the install.
- Shared parts that another profile still uses.
- A FreeLLMAPI that Polytropos did not install. Polytropos leaves it alone.
- The `FREELLMAPI_API_KEY` line that `hermes config set` wrote to the profile's `.env`.
- The changes that FreeLLMAPI's `setup-opencode` made for OpenCode. It added the `freellmapi` provider and a default model to `~/.config/opencode/opencode.json`, and stored FreeLLMAPI's key under `freellmapi` in `~/.local/share/opencode/auth.json`. Uninstall prints both paths, and you change them by hand.
- Polytropos's own files in `~/.local/state/polytropos`, `~/.cache/polytropos` and `~/.local/share/polytropos/canary`.

## What Polytropos runs and changes

This section lists what Polytropos does outside its own folders, so that you can judge it before you install.

- **Background processes.** In managed mode FreeLLMAPI runs as a LaunchAgent on macOS or a systemd user unit on Linux, and listens on 127.0.0.1:3001. While a turn runs, the polytropos plugin keeps the machine awake with `caffeinate -i` on macOS or `systemd-inhibit` on Linux. That process stops when the turn ends, and after two hours at the latest.
- **Commands.** The `hermes polytropos` commands run git, npm, npx, uv, `hermes config set`, `hermes mcp add`, `hermes plugins install`, and `launchctl` or `systemctl --user`, plus the install commands of the programs in `versions.lock`. The doctor runs the version commands of those programs, and `pmset -g` on macOS. During a turn the memory guard runs `sysctl` on macOS and reads `/proc` on Linux. Polytropos never runs `sudo`. On Linux it does not install Ollama, because Ollama's install script asks for `sudo`, and prints that script's command instead.
- **Files outside its own folders.** Setup and install change the profile's `config.yaml` through `hermes config set` and record the old values for uninstall. Install adds Hermes skills, rtk's Hermes plugin and MCP entries to the profile, a LaunchAgent in `~/Library/LaunchAgents` or a unit in `~/.config/systemd/user`, programs in `~/.local/bin`, and after you say yes, ponytail through `hermes plugins install`. The doctor reads Hermes's plugin install records, the plugin folders of the other profiles, and the names of files in `~/Downloads`, `~/Desktop` and the home folder.
- **Your own FreeLLMAPI.** `hermes polytropos adopt --from <checkout>` copies the database, `.env` and `.encryption-key` of the FreeLLMAPI checkout you name into Polytropos's data folder, so that FreeLLMAPI can still read its provider keys. Polytropos reads the encryption key only to write it into the `.env` of the FreeLLMAPI it runs.
- **Credentials.** The plugins read `FREELLMAPI_API_KEY`, and the key variable of any other upstream you add to the router, through Hermes's secret scope for the active profile. Provider keys stay in FreeLLMAPI's encrypted database. npm, npx and uv run with a short list of environment variables, so the scripts of the packages they install do not see the keys in Hermes's environment.
- **Network.** The router sends each turn to the models on its routes. By default those are FreeLLMAPI and Ollama on this machine, and FreeLLMAPI passes cloud turns on to the providers whose keys you added. The router and the memory guard ask the Ollama API for its version, its models and the models in memory. The last item of the next section lists the rest.
- **Telemetry.** Polytropos sends no usage data anywhere.

## What stays on this machine

- In managed mode FreeLLMAPI listens on 127.0.0.1 only.
- Provider keys stay in FreeLLMAPI's encrypted database, and the unified key in the profile's `.env`. Polytropos does not print or log key values, and it records no `model.api_key`. The setup of Polytropos 0.1.1 kept a copy of the whole `model` section in `~/.local/state/polytropos/state.json`, with mode 0600, API key included. The first `hermes polytropos` command that reads that file splits the copy into single keys, drops the API key and saves the file. Uninstall does not write the key back.
- The router writes no message text to disk. Its traces are off by default. When you turn them on, they record the route and the model of each request without message text, in a file that only your user can read.
- The classifier sends the last user message of each turn to the classifier model, which is FreeLLMAPI's `auto` model by default. It sends at most 2,000 characters, the start and the end of a longer message. Hermes's own auxiliary calls, such as titles, context compression and smart approvals, also pass through the router, and each of them costs one classifier call. The user message of a compression call holds the conversation being summarized. With a cloud classifier that text reaches a cloud provider, also when the turn itself then runs on a local model.
- `hermes polytropos canary run` only tests endpoints on this machine, and its results stay in `~/.local/share/polytropos/canary`.
- The doctor's key-file check reads file names and permissions and does not open the files.
- Programs are installed at the versions in `versions.lock`. The FreeLLMAPI checkout is checked against its pinned commit, and the rtk and macOS Ollama archives against their SHA-256.
- Outside installs and updates, Polytropos contacts GitHub only for stack-canary's public results. It downloads them at most once a day while its copy is good, and at most once an hour after a failed download. Installs and updates download from GitHub, npm and PyPI.

## WhatsApp pacing

The repository also holds `whatsapp-human-pace`, a plugin for Hermes's WhatsApp gateway. It holds incoming WhatsApp messages and answers after a random delay of 5 to 10 minutes. Text messages that arrived one after another in the meantime are joined into one turn, and a message with media, such as a photo, goes on as a message of its own. In WhatsApp replies the plugin turns Markdown into plain text, replaces long dashes with commas, straightens curly apostrophes, and removes most emoji, curly double quotes and a few Dutch stock openers. By default the first reply in a chat each day starts with the note "Replies in this chat come from an assistant." The plugin remembers that note per Hermes session in memory, so the note comes again after a gateway restart, a session reset or a context compression. `/pace` also takes the Dutch words `uit`, `aan` and `nu`, and the list of stock openers is Dutch. Both are kept on purpose. The plugin is not part of the pack. Install it on its own with

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
