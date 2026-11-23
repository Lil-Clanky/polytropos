# Polytropos

This plugin adds the `hermes polytropos` command to [Hermes Agent](https://github.com/NousResearch/hermes-agent). It sets up a Hermes profile to use free cloud models through [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) and small local models through [Ollama](https://ollama.com), on macOS and Linux. It installs the other parts at pinned versions, runs FreeLLMAPI as a background service and checks that everything works together. During a turn it pauses work on a local model when memory runs low, and keeps the machine awake while the turn runs.

It works together with the [polytropos-router](../polytropos_router/README.md) plugin, the model provider that picks a model for each turn.

## Install

```sh
hermes plugins install polytropos
hermes plugins install polytropos-router
hermes polytropos doctor
hermes polytropos install
hermes config set FREELLMAPI_API_KEY <unified key>
hermes polytropos setup
```

The doctor reports what is still missing. `install` builds FreeLLMAPI at its pinned tag, runs it on `127.0.0.1:3001` and installs the programs. It asks before it installs ponytail and before each optional program. Before `setup`, open FreeLLMAPI's dashboard at http://127.0.0.1:3001, add your provider keys and copy its unified API key. `hermes config set` stores it in the profile's `.env`, and `setup` stops without it. Run the doctor again afterwards. It can ask whether to make the router the default model.

## Requirements

- Hermes Agent v0.23.2 or newer.
- macOS, or Linux with systemd.
- git, [uv](https://docs.astral.sh/uv/), and Node.js `>=20.18.0 <25.0.0` with npm.
- `~/.local/bin` on your `PATH`.

## What it runs and changes

- FreeLLMAPI runs as a LaunchAgent on macOS or a systemd user unit on Linux, and listens on 127.0.0.1 only.
- The commands run git, npm, npx, uv, `hermes config set`, `hermes mcp add`, `hermes plugins install`, and `launchctl` or `systemctl --user`. Polytropos never runs `sudo`. On Linux it prints Ollama's install command for you to run.
- Setup changes the profile's `config.yaml` and records the old values, so that `hermes polytropos uninstall` can put them back.
- `hermes polytropos adopt` copies the database, `.env` and `.encryption-key` of a FreeLLMAPI checkout you name, so that FreeLLMAPI can still read its provider keys.
- Keys are read through Hermes's secret scope. Polytropos sends no usage data anywhere.

The full list is in [What Polytropos runs and changes](../../README.md#what-polytropos-runs-and-changes). Every command, setting and path is in the [reference](../../docs/reference.md).

## Updates

`hermes plugins update` moves this plugin to the commit the catalog pins. `hermes polytropos update` then brings ponytail, the programs and FreeLLMAPI to the versions this plugin pins.

## License

MIT, see [LICENSE](../../LICENSE).
