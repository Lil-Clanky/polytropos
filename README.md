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
- Node.js `>=20.18.0 <25.0.0` with npm, to build and run FreeLLMAPI. Polytropos does not install Node.
- `~/.local/bin` on your `PATH`. Polytropos puts rtk there, and on macOS also Ollama.

## Install

Run these commands in a terminal, in the Hermes profile you want to set up.

```sh
git clone https://github.com/Lil-Clanky/polytropos
hermes plugins pack install polytropos/hermes-pack.yaml
hermes polytropos doctor
hermes polytropos install
hermes polytropos setup
hermes polytropos doctor
```

Here is what each step does.

1. **Get the pack file.** Clone the repository, or download `hermes-pack.yaml` from a release and install that file.
2. **Install the pack.** Hermes shows its review screen and asks you to confirm with `y`, which is why the pack install needs a terminal. It installs polytropos, polytropos-router and superpowers. On Hermes v0.23.2 it also prints a failure line for ponytail and exits with status 1. Step 4 installs ponytail another way. When the install stops with `fatal: shallow file has changed since we read it`, run the same command again. To install the pack over an earlier install, add `--force`.
3. **Run the doctor.** On a new install it reports FreeLLMAPI, ponytail and the programs as missing.
4. **Install the rest.** `hermes polytropos install` builds FreeLLMAPI at its pinned tag and runs it as a service on `127.0.0.1:3001`. It asks before it installs ponytail from its pinned commit, installs the default programs, and asks about each optional one. When a FreeLLMAPI already answers on port 3001, Polytropos uses that one as it is and leaves its updates to you. To let Polytropos run it instead, stop it and run `hermes polytropos adopt --from <its checkout>`. When another program holds port 3001, install stops and asks you to free the port first.
5. **Add provider keys.** Open FreeLLMAPI's dashboard at http://127.0.0.1:3001, add your provider keys on its Keys page, and copy the unified API key from the header of that page.
6. **Connect Hermes.** `hermes polytropos setup` runs FreeLLMAPI's own `setup-hermes`. When it asks for an API key, paste the unified key. `setup-hermes` stores the key as `FREELLMAPI_API_KEY` in the profile's `.env` and makes FreeLLMAPI's model the default. Polytropos then asks whether to make the router the default model instead, and writes the router settings.
7. **Run the doctor again.** When no check fails and the router is not the default model yet, the doctor asks whether to make it the default.

### The FreeLLMAPI key

Hermes needs `FREELLMAPI_API_KEY` for every turn on the router, also when all routes are local. Without it Hermes stops the turn with an error that starts with `No usable credentials found for provider 'polytropos-router'. Set FREELLMAPI_API_KEY.` When a fallback provider is configured (see `hermes fallback`), Hermes runs the turn on the fallback instead, without the router. `hermes polytropos setup` leaves the key in the profile's `.env`.

### Restarting the gateway

A running gateway keeps the old plugin code until it restarts. Hermes prints `hermes gateway restart` after a pack install, and `hermes polytropos update` and `uninstall` print it when they change plugins. Polytropos never restarts the gateway itself, because a restart interrupts running chats.

### Profiles

`hermes polytropos` works on the active Hermes profile. Plugins, settings and the FreeLLMAPI key belong to a profile, so run the install steps in each profile that should use Polytropos, for example `hermes --profile <name> polytropos setup`. FreeLLMAPI and the programs are shared by the whole machine. Polytropos records which profiles use each shared part, and an uninstall from one profile removes a shared part only when no other profile uses it.

## What the doctor checks

`hermes polytropos doctor` prints one line per finding, marked `ok`, `warn`, `fail` or `info`, with a fix on the next line where it has one. It exits with status 1 when a check fails. It looks at the following.

- The Hermes version.
- The pack plugins, against the commits pinned in `versions.lock` and the commit of the installed polytropos plugin.
- Whether FreeLLMAPI answers on 127.0.0.1:3001, and in which mode Polytropos runs it.
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

`update` reads the latest Polytropos release from GitHub, shows what would change and asks before it changes anything. It updates the plugins through the release's pack file, so Hermes shows its review screen again. Then it updates the programs, and FreeLLMAPI when Polytropos runs it. `hermes polytropos update programs` and `hermes polytropos update freellmapi` update one part only.

Before a FreeLLMAPI update, Polytropos prints the installed tag, the new tag and a GitHub link that compares the two. It builds the new version next to the running one, stops the service, backs up the database and keys, starts the new version and checks it. When the check fails, it puts the backup and the old version back. To go back by hand later, run `hermes polytropos rollback freellmapi`. It asks first, because a rollback loses the changes made in FreeLLMAPI since the update.

Polytropos does not update anything until you run `update`.

## Uninstall

```sh
hermes polytropos uninstall
```

Uninstall sets the settings that Polytropos changed back to their old values and undoes the integrations that `install` recorded, such as the Hermes skills and the context7 MCP entry. It removes the FreeLLMAPI service and the programs that Polytropos installed, once no other profile uses them. At the end it prints a `hermes plugins remove` command for each pack plugin. Run those yourself, then `hermes gateway restart`.

Some things stay.

- FreeLLMAPI's database, the provider keys and the update backups, in `~/.local/share/polytropos/freellmapi`. `hermes polytropos uninstall --purge-data` deletes that folder after you type `yes`.
- Programs that were on the machine before Polytropos.
- Shared parts that another profile still uses.
- A FreeLLMAPI that Polytropos did not install. Polytropos leaves it alone.
- What FreeLLMAPI's `setup-hermes` wrote. It ran before Polytropos recorded any change, so after an uninstall the `FREELLMAPI_API_KEY` line is still in `.env` and Hermes's default model is FreeLLMAPI's own model. The model you used before the setup does not come back. If the uninstall removed FreeLLMAPI, choose another model with `hermes model`.
- The FreeLLMAPI provider in OpenCode's own config, which FreeLLMAPI's `setup-opencode` added. Uninstall prints what to change there.
- Polytropos's own files in `~/.local/state/polytropos`, `~/.cache/polytropos` and `~/.local/share/polytropos/canary`.

## What stays on this machine

- In managed mode FreeLLMAPI listens on 127.0.0.1 only.
- Provider keys stay in FreeLLMAPI's encrypted database, and the unified key in the profile's `.env`. Polytropos does not print or log key values.
- The router writes no message text to disk. Its traces are off by default. When you turn them on, they record the route and the model of each request without message text, in a file that only your user can read.
- The classifier sends the last user message of each turn to the classifier model, which is FreeLLMAPI's `auto` model by default. With a cloud classifier that text reaches a cloud provider, also when the turn itself then runs on a local model.
- `hermes polytropos canary run` only tests endpoints on this machine, and its results stay in `~/.local/share/polytropos/canary`.
- The doctor's key-file check reads file names and permissions and does not open the files.
- Programs are installed at the versions in `versions.lock`. The FreeLLMAPI checkout is checked against its pinned commit, and the rtk and macOS Ollama archives against their SHA-256.
- Outside installs and updates, Polytropos contacts GitHub only for stack-canary's public results, at most once a day. `update` reads the latest release from GitHub. Installs and updates download from GitHub, npm and PyPI, and for Ollama on Linux from ollama.com.

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

CI runs these on Ubuntu and macOS with Python 3.11 and 3.13. Code that both plugins use lives in `shared/polytropos_shared/`. `scripts/sync_shared.py` copies it into each plugin's `_shared/` folder and copies `versions.lock` into the polytropos plugin, and `--check` exits with status 1 when a copy is out of date. The copies are committed, because Hermes installs a plugin folder straight from git. A test run refreshes them as well. Tests marked `e2e` need a real Hermes install and run only locally.

How a release is made and how the weekly update job works is described in the maintainer section of the reference.

## License

MIT, see [LICENSE](LICENSE). Polytropos installs the other parts from their own sources under their own licenses, which `versions.lock` lists.
