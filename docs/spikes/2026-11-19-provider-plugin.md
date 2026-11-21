# Provider plugin spike, 2026-11-19

This spike ran Hermes v0.23.2 (`~/.hermes/hermes-agent`, git checkout, read only) with a minimal
provider plugin and a hook probe in isolated homes. It answers how the router plugin and the
general plugin are built. File references such as `providers/__init__.py:588` point into the Hermes
checkout. Log lines come from the spike plugins, which append JSON lines to
`<HERMES_HOME>/spike.jsonl`.

## Result

The provider plugin loads, streams and runs a full tool loop in all three placements: flat,
nested under `model-providers/`, and installed by a pack from a local git repository. The router
stays a provider plugin and the proxy fallback is not needed.

One safety check failed. After the last Hermes run, `launch_snapshot()` showed one new launchd
label, `com.nousresearch.hermes.runtime.<hex>`. Hermes created it for the sandbox home
when a one-shot run started its background gateway. See "Launchd difference" below. By the
controller's ruling the job stays in place, and Task 24 leaves such labels out of its comparison.

## How the spike ran

- `spikes/provider_min/` is the provider `polytropos-spike` (`kind: model-provider`). Its
  `create_client(**kw)` returns a facade in the shape of `agent.acp_client.ACPClient` that forwards
  every call to an `openai.OpenAI` client for the loopback fake server. The upstream URL comes from
  `plugins.entries.polytropos-spike.upstream` in `config.yaml`. A `_shared/` subpackage tests
  relative imports.
- `spikes/hook_probe/` is the general plugin `hook-probe`. It logs `pre_llm_call`,
  `pre_api_request`, `post_llm_call` and `on_session_end` and returns `HOOK-PROBE-CONTEXT` as
  `pre_llm_call` context.
- `spikes/run_spike.py ROOT PLACEMENT` starts `tests/fake_openai.py`, scripts a `write_file` call
  (`{"path": "out.txt", "content": "hi"}`) followed by the text `Wrote out.txt.`, and runs
  `hermes -z "Create out.txt with the text hi." --provider polytropos-spike -m auto` through
  `scripts/hermes_sandbox.py`. `-z` is the one-shot flag from `hermes --help`.
- Each run gets `HERMES_HOME=<root>/hermes`, `HOME=<root>/home`, `PYTHONDONTWRITEBYTECODE=1` and
  only an allowlist of the parent environment, so no real API key reaches Hermes. The sandbox
  `.env` holds two fake values, `POLYTROPOS_SPIKE_KEY=spike-key-1` and
  `POLYTROPOS_SPIKE_OTHER=spike-other-2`. Since fix round 1, `sandbox_env` also sets
  `TMPDIR=<root>/tmp`. The spike runs had the parent's `TMPDIR`, so the frozen provider copies of
  finding 1 landed outside the sandbox.

| Placement | Exit | `out.txt` | Requests at the fake server |
|---|---|---|---|
| `plugins/polytropos-spike/` | 0 | `hi` | 2, both `Bearer spike-key-1`, `stream: true` |
| `plugins/model-providers/polytropos-spike/` | 0 | `hi` | 2, same |
| pack install from a local git repository | 0 | `hi` | 2, same |
| flat, no `.env` | 2 | none | 0 |

## Findings

### 1. Placement and package loading

Both folders load. `_scan_home_layer` imports `$HERMES_HOME/plugins/model-providers/<name>/`
first, then every flat `$HERMES_HOME/plugins/<name>/` whose `plugin.yaml` says
`kind: model-provider` (`providers/__init__.py:588-614`). A pack or `hermes plugins install` puts
the plugin in the flat folder, so the flat placement is the one to use.

The plugin loads as a package. The module name is per home
(`_hermes_user_provider_<sha1 of home>_<folder>`, `providers/__init__.py:633-635`) and the spec has
`submodule_search_locations=[plugin_dir]` (`providers/__init__.py:720-726`), so `from ._shared
import MARKER` works. Hermes imports a frozen copy of the plugin tree from a temporary folder, so
`__file__` points there. The whole tree is copied, `_shared/` included:

```
{"event": "import", "module": "_hermes_user_provider_293f233d0e_polytropos_spike_b9963ce985df",
 "file": "$TMPDIR/hermes-provider-plugins-2u3jktz3/_hermes_user_provider_293f233d0e_polytropos_spike_b9963ce985df/polytropos-spike/__init__.py",
 "relative_import": "relative import ok", "pid": 16299, "thread_name": "MainThread"}
```

Two processes import it in every run: the `hermes -z` process (pid 16299) and the gateway daemon
that the one-shot run starts (pid 16320). The turn itself runs in the daemon (see "Other
observations").

A general plugin is imported in place as `hermes_plugins.<key>`, with `/` and `-` replaced by
`__` and `_` (`hermes_cli/plugins_loader.py:795-799`, `:814-841`). Hermes adds no plugin folder to
`sys.path`, so neither plugin can be imported by its folder name, and after a pack install the
router's folder is `polytropos-router`, which is no Python identifier. The doctor imports
`polytropos_router.config` by package name. A stub package makes that name resolve without
running the router's `__init__.py`, which would call `register_provider` again: a
`types.ModuleType("polytropos_router")` with `__path__` set to the router's folder, placed in
`sys.modules`, after which `importlib.import_module("polytropos_router.config")` loads `config.py`
and its relative `_shared` import. This was checked with Python 3.11.15 on a copy of that layout
whose `__init__.py` raises; it was not checked inside Hermes. The folder name differs by install
path (`polytropos-router` after a pack install, `polytropos_router` when `make_home` copies the
source folder), so the stub looks for the folder whose `plugin.yaml` has `name: polytropos-router`.

### 2. `base_url`, `client_kwargs` and the key

`base_url="polytropos://router"` is accepted. It reaches `create_client` and the hook kwargs
unchanged, and the runs logged no error about it. `client_kwargs` has `api_key` and `base_url`, plus
`max_retries: 0` for the client made for one API request. `api_key` carries the value of the
profile's `env_vars` entry, read from the sandbox `.env`:

```
{"event": "create_client", "client_kwargs": {"api_key": "spike-key-1", "base_url": "polytropos://router"}, "thread_name": "check-fn-probe"}
{"event": "create_client", "client_kwargs": {"api_key": "spike-key-1", "base_url": "polytropos://router"}, "thread_name": "hermes-gateway_1"}
{"event": "create_client", "client_kwargs": {"api_key": "spike-key-1", "base_url": "polytropos://router", "max_retries": 0}, "thread_name": "Thread-9 (<lambda>)"}
```

`create_client` ran 4 times per turn in every successful run: twice from tool availability probes
(`check-fn-probe`, `tools/registry.py:427`), once for the agent (`hermes-gateway_1`) and once for
the first API request. The second request reused a client. After the turn, `close()` ran twice on
`hermes-gateway-hk_0`; the two probe clients got no `close()` call.

Without the key the turn never starts. With no `.env`, the run printed
`No usable credentials found for provider 'polytropos-spike'. Set POLYTROPOS_SPIKE_KEY.`, exited
with 2, and `create_client` was never called. The rule is in `hermes_cli/runtime_provider.py:1072-1078`:
a provider with `auth_type="api_key"` (the `ProviderProfile` default, `providers/base.py:165`)
needs a usable secret. The other auth types do not fit the router: `external_process` needs an
external command (`hermes_cli/auth.py:2239-2254`).

### 3. The `create` call and the returned stream

Hermes streams. Each call has `stream=True` and these kwargs and nothing else:

```
{"event": "create", "model": "auto", "stream": true,
 "kwargs": ["messages", "model", "stream", "stream_options", "timeout", "tools"],
 "timeout": "Timeout(connect=30.0, read=1800.0, write=1800.0, pool=30.0)", "roles": ["system", "user"]}
```

`stream_options` was `{"include_usage": true}` at the fake server. `timeout` is an `httpx.Timeout`.

Attributes Hermes touched on the returned stream, in order: `choices` (missing, so Hermes treats the
object as a stream), `response`, `__iter__`, `close`. Hermes reads chunks with `yield from stream`
and `.response` through `getattr(stream, "response", None)` (`agent/chat_completion_helpers.py:285-291`),
so `.response` is optional. Any returned object with a `choices` attribute is taken as a finished
non-streamed response: the main path checks `hasattr(value, "choices")`
(`agent/chat_completion_helpers.py:3056`), and only the Bedrock path also wants it non-empty
(`:2596`). So the stream must have no `choices` attribute; an empty `choices` also marks a
finished response.

Attributes Hermes asked for on the client and did not find: `_client` (6 times per turn),
`_transport` and `_mounts` (3 times each). Hermes looks for an httpx client to force-close sockets
on an interrupt (`agent/agent_runtime_helpers.py:3440-3455`, `agent/process_bootstrap.py:107`).
All three reads use defaults, so the facade works without them, and Hermes's interrupt cannot
close the router's upstream sockets. The router's own watchdog has to do that.

Spec spike 1 also asked about the class flags `HERMES_SKIP_TRANSPORT_WRAP` and
`HERMES_SKIP_ASYNC_WRAP`, which `agent.acp_client.ACPClient` sets (`agent/acp_client.py:280-281`).
Only Hermes's auxiliary client reads them (`agent/auxiliary_client.py:1884`, `:4529`); the spike
never exercised that path, and the main path reads `_transport` and `_mounts` with defaults.

### 4. Message roles across the tool loop

```
call 1: ["system", "user"]
call 2: ["system", "user", "assistant+tool_calls", "tool"]
```

The fake server saw the same two lists. The turn rule holds: a new decision when the last message
is from `user`, the same decision while it is `tool`.

### 5. Threads, `pre_llm_call` kwargs and where the context goes

`pre_llm_call` and `pre_api_request` run on a thread named `hermes-hook-callback`. Each API call
runs on its own new thread (`Thread-9 (<lambda>)`, then `Thread-13 (<lambda>)`). Thread idents are
reused: in the first run the hook thread and the API thread both had ident 13069971456 with
different names. Correlating hook and call by thread id is not reliable.

`pre_llm_call` fires once per turn with these kwargs (`agent/turn_context.py:920-932`, plus
`telemetry_schema_version` from the dispatcher): `conversation_history`, `is_first_turn`, `model`,
`parent_session_id`, `platform`, `sender_id`, `session_id`, `task_id`,
`telemetry_schema_version`, `turn_id`, `user_message`. It has no `provider` and no `base_url`.

`pre_api_request` fires once per API call (`agent/turn_api_request.py:84-109`) and has `provider`,
`base_url` and `api_mode`, plus `api_call_count`, `api_request_id`, `approx_input_tokens`,
`conversation_history`, `max_tokens`, `message_count`, `middleware_trace`, `model`, `platform`,
`request`, `request_char_count`, `request_messages`, `retry_count`, `session_id`, `started_at`,
`system_prompt`, `task_id`, `telemetry_schema_version`, `tool_count`, `turn_id`, `user_message`.
Its return value is not used. Task 16 therefore records `provider`, `base_url` and `model` per
session in `pre_api_request` and uses that record at the session's next `pre_llm_call`.

Context returned as `{"context": "..."}` is appended to the user message with a blank line
between them. The request's last user message was
`"Create out.txt with the text hi.\n\nHOOK-PROBE-CONTEXT"` in both calls of the turn. The code
joins memory prefetch and plugin context with `\n\n` and appends them to a string message
(`agent/turn_context.py:112-135`, `:965`); a list (multimodal) message gets them as a separate text
part. The stored `user_message` is therefore a prefix of the request's last user message, which
is what the bridge in Task 3 assumes. `{"action": "block", "message": ...}` vetoes the turn.

### 6. Pack install of a subfolder

The pack file used `repo: file:///.../spike-repo.git`, an exact 40-character `ref` and
`subdir: spikes/provider_min` and `subdir: spikes/hook_probe`. `plugin.yaml` needed no extra field:
the spike manifests have only `name`, `kind`, `version`, `description`, `author` (and
`provides_hooks` for the probe). `manifest_version` is optional and, when present, must be an
integer up to 2 (`pm/plugin_declarations.py:36-51`, `hermes_cli/plugins_manifest.py:42`).
Unknown fields never stop a load: a v1 manifest (no `manifest_version`) logs them at debug level,
a v2 manifest at warning level (`hermes_cli/plugins_manifest.py:192-198`). `provides_commands` is
one of them.

Manifest v2 (spec line 63, checked in fix round 1 against the source and with Hermes's own parser):

- A v2 manifest needs only `manifest_version: 2`; every other field stays optional
  (`hermes_cli/plugins_manifest.py:162-203`, `website/docs/developer-guide/plugins/index.md:611-621`).
  `api_version` is the plugin API generation, `None` meaning the current one
  (`hermes_cli/plugins_manifest.py:393-396`); only the plugin checker looks at it and warns below 1
  (`hermes_cli/plugin_dev.py:351-353`).
- `kind` and `provides_hooks` are known fields (`hermes_cli/plugins_manifest.py:32-39`). `kind`
  takes `standalone`, `backend`, `exclusive`, `platform` or `model-provider` (`:27`). When it is
  missing, Hermes guesses it from the text of `__init__.py`: a file that contains both
  `register_provider` and `ProviderProfile` counts as a model provider (`:287-298`, `:529-548`).
  The general loader skips a model provider (`hermes_cli/plugins_discovery.py:309-311`), and
  provider discovery reads only a declared `kind` (`providers/__init__.py:551-585`), so a general
  plugin guessed that way would not load at all. The general plugin therefore declares
  `kind: standalone`.
- `provides_hooks` only describes the plugin: Hermes stores it (`hermes_cli/plugins_manifest.py:577`),
  shows it in the catalog (`hermes_cli/plugin_catalog.py:596`) and its plugin checker reports
  unknown hook names (`hermes_cli/plugin_dev.py:457-470`); `register_hook` does not consult it.
- `requires_plugins` is advisory and orders `register()` calls among the plugins the general loader
  loads (`hermes_cli/plugins.py:540-545`). The router is a model provider and never in that set, so
  `requires_plugins: [{id: polytropos-router}]` on the general plugin logs
  `Plugin polytropos requires plugin 'polytropos-router' which is not enabled/installed; loading anyway`
  on every load (`hermes_cli/plugins_manifest.py:255-262`) and orders nothing. Neither manifest
  declares it.

The in-process check ran `parse_manifest_file`, `manifest_version_error` and
`providers._declares_model_provider_kind` from the Hermes venv on temporary copies of the two
manifests that Tasks 10 and 17 now give. Both parsed as `manifest_version: 2`, `api_version: 1`,
with `kind: model-provider` for the router and `kind: standalone` for the general plugin; the
installer check returned no error; provider discovery accepted only the router. The same general
manifest without `kind`, next to an `__init__.py` whose comment names `register_provider` and
`ProviderProfile`, parsed as `kind: model-provider`. A v2 manifest with `provides_commands` logged
a warning, a v1 manifest with it a debug line. No Hermes command ran.

The installed folder is `plugins/<manifest name>/`, else the subdir's last part
(`hermes_cli/plugins_cmd_install.py:448-453`). Only the subfolder is copied:

```
hermes/plugins/.install-metadata.json
hermes/plugins/hook-probe/{__init__.py, plugin.yaml}
hermes/plugins/polytropos-spike/{__init__.py, _shared/, plugin.yaml}
```

`.install-metadata.json` records each plugin with `"pinned": true`, the revision and
`"source": "file:///.../spike-repo.git#spikes/provider_min"`.

The identifier is `<repo>/<subdir>`; Hermes splits a URL at `.git/` or `#`
(`hermes_cli/plugins_cmd.py:303-329`), so a local repository folder has to end in `.git`.
`owner/repo` with a subdir becomes `https://github.com/owner/repo.git` plus the subdir.

The install needs a terminal (`hermes_cli/plugin_packs.py:537-543`, no `--yes`). It asked one
question, the pack review. Rich drops `[y/N]` from the printed prompt, so the prompt ends with
`Install 2 plugin(s) from pack 'polytropos-spike-pack'?`. The driver ran it on a pseudo terminal
and typed `y` when the output paused without a newline. No per-plugin consent prompt came. The
security scan passed both plugins (`agent.log`: `plugin scan passed for provider_min: Allowed
(clean scan)` and the same for `hook_probe`). The install ends with
`Restart the gateway for the plugins to take effect:` and `hermes gateway restart`.

The pack enabled both plugins: `plugins.enabled: [hook-probe, polytropos-spike]`.

### 7. ponytail

ponytail does not install on Hermes v0.23.2. A pack install exits 1 when any entry fails
(`hermes_cli/plugin_packs.py:554-555`), so on that version every install of the Polytropos pack
exits 1 (derived from the source; the spike installed ponytail on its own).

At `7801d061243d7b753bad0e0b3c057cd250b11755` (tag v5.3.3, from `versions.lock`), after a
warning that the source is custom and unreviewed:

```
Cloning https://github.com/DietrichGebert/ponytail.git...
Error: Plugin 'ponytail' is unavailable: plugin.json declares an unsupported or
missing Agent Plugins schema.
```

The security scan passed (`agent.log`: `plugin scan passed for plugin: Allowed (clean scan)`). The
repository root has `plugin.yaml` and a `plugin.json` that holds only `{"name": "ponytail"}`.
During install Hermes validates any `plugin.json` as an Agent Plugins package
(`hermes_cli/plugins_cmd_install.py:300-314`, called at `:497`), and a missing `$schema` fails
(`hermes_cli/agent_plugins.py:239-240`). This happens even though `plugin.yaml` is there. The
newest commit, `96608e078b2d5ea607c5c3b8b5a54d1c1fba6846` (the tip of the default branch), has the
same `plugin.json` (read with plain git), so it fails the same check.

Both install attempts at the newest commit stopped earlier, at the fetch:

```
Error: Git commit '96608e078b2d5ea607c5c3b8b5a54d1c1fba6846' could not be
fetched:
fatal: shallow file has changed since we read it
```

Hermes clones with `git clone --depth 1 --no-checkout`, adds `--filter=blob:none` when the entry
has a `subdir`, and then runs `git fetch --depth 1 origin <sha>`
(`hermes_cli/plugins_cmd_git.py:231-233`, `:166`). It does not retry. Plain git 2.50.1 fails the
same way now and then. The first plain-git checks against ponytail looked like a rule (the tip
failed, an older commit and a partial clone passed), but repeated runs of the same two commands
against obra/superpowers failed 3 times in 14: once at the tip, once at an older commit and once
with `--filter=blob:none`. A later plain-git fetch of the ponytail tip passed. The failure is
intermittent and its cause was not found, so any git entry of a pack can hit it. Running the pack
install again retries the entry; the plugins that did install then fail with
`Plugin '<name>' already exists` unless the install is forced
(`hermes_cli/plugins_cmd_install.py:499-505`).

superpowers, the other third-party entry of the pack, was checked from its repository and the
Hermes source only; the spike did not install it. At the pinned commit
`22f0b463e7a5e579904dd6211a520321035526f7` (v7.0.2, also the tip of its default branch during the
spike) the repository root has no `plugin.yaml` and no `plugin.json`. Its Hermes manifest is
`.hermes-plugin/plugin.yaml` (`name: superpowers`, `provides_hooks: [pre_llm_call]`), and its
`register` looks for the `skills/` tree next to `.hermes-plugin/` or inside it and raises when
neither exists. Hermes v0.23.2 has no special case for `.hermes-plugin/`. An entry with
`subdir: .hermes-plugin` copies only that folder, so `register` raises. An entry without `subdir`,
as in the spec, installs the repository as `plugins/superpowers/`. Discovery finds no manifest
there, looks one level down (`hermes_cli/plugins_discovery.py:141-186`) and reads
`.hermes-plugin/plugin.yaml` as the plugin `superpowers/.hermes-plugin` named `superpowers`
(`hermes_cli/plugins_manifest.py:564-565`). The pack enables `superpowers`
(`hermes_cli/plugin_packs.py:399`), and the enable check accepts the key or the name
(`hermes_cli/plugins_discovery.py:275-278`, `:320`). General plugins are imported in place
(`hermes_cli/plugins_loader.py:834-835`), so `skills/` sits next to `.hermes-plugin/` and this
entry should load. Whether the install's security scan admits the whole repository was not
checked.

### 8. Packs and pip packages (spec spike 3)

A pack cannot install a pip package. An entry has `name` (catalog) or `repo` (git), an exact
`ref` and an optional `subdir` (`hermes_cli/plugin_packs.py:176-203`), and every entry goes through
the git install path (`hermes_cli/plugin_packs.py:379-384`). `hermes plugins install` accepts a
catalog name, a git URL or `owner/repo` (its `--help`). Hermes discovers providers from
`hermes_agent.plugins` entry points (`providers/__init__.py:3-8`), but only for packages already
in its environment, and that environment belongs to its package manager. The `_shared/` copies stay.

### 9. Pack config seeds and how a plugin reads its entry

Seeds land at the top level of `plugins.entries.<installed name>`. After the pack install:

```yaml
plugins:
  enabled: [hook-probe, polytropos-spike]
  disabled: []
  entries:
    polytropos-spike:
      upstream: http://127.0.0.1:57237/v1
    hook-probe:
      probe: seeded-top-level
      settings:
        probe: seeded-settings
```

A seed key is written only when the entry does not have it yet; the check is per top-level key, so
an existing `settings` mapping keeps the whole seeded `settings` out
(`hermes_cli/plugin_packs.py:318-338`). Seeds may not use `granted_capabilities`,
`capabilities_consent`, `allow_*` or a key matching
`(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key|credential|auth)` at any depth
(`hermes_cli/plugin_packs.py:21-23`, `:79-85`, `:88-90`, `:117-138`).

A general plugin reads its entry with `ctx.get_config(key)`, which reads
`plugins.entries.<plugin id>.settings.<key>` and then the legacy `config` subtree
(`hermes_cli/plugins_context.py:75-85`). Top-level seed keys are not visible to it. The probe
got `"get_config_probe": "seeded-settings"` after the pack and `"entry-settings"` in the manual
placements, where the config had both a top-level `probe` and `settings.probe`. The plugin id is
the manifest name (`"plugin_id": "hook-probe"` while the folder was `hook_probe`), and
`plugins.enabled` takes that name.

A provider plugin has no `ctx`. The spike provider read its entry at run time with
`hermes_cli.config.load_config()` inside `create_client` and got the seeded
`upstream: http://127.0.0.1:57237/v1`. The router keeps that import inside its functions. The
first version of this finding said discovery runs while `hermes_cli.config` is still importing;
neither the source nor the logs show that on v0.23.2, where `hermes_cli/config_provider_env.py:1-8`
says that import path was removed, so the reason is dropped.

### 10. `hermes config set` with nested keys, lists and mappings

Nested keys, lists and mappings under `plugins.entries` can be set. Only the list write ran
without `--force`; the mapping and number writes ran with it:

```
$ hermes config set plugins.entries.polytropos-router.routes.simple '["freellmapi/auto"]'
✓ Set plugins.entries.polytropos-router.routes.simple = ['freellmapi/auto'] in .../config.yaml
$ hermes config set --force plugins.entries.polytropos-router.settings '{"default_route": "complex", "awake": true}'
✓ Set plugins.entries.polytropos-router.settings = {'default_route': 'complex', 'awake': True} in .../config.yaml
$ hermes config set --force plugins.entries.polytropos-router.timeout_s 30
✓ Set plugins.entries.polytropos-router.timeout_s = 30 in .../config.yaml
```

Stored as:

```yaml
    polytropos-router:
      routes:
        simple:
          - freellmapi/auto
      settings:
        default_route: complex
        awake: true
      timeout_s: 30
```

All calls exited 0. Values are coerced: words to bool or None, numbers to int or float, list and
mapping literals through `yaml.safe_load`; keys whose default is a string stay strings
(`hermes_cli/config.py:2943-2968`). `--force` is needed only for a known key under the wrong prefix
(`hermes_cli/config.py:3153-3208`). That the mapping and number writes also work without `--force`
rests on those source lines; no run checked it.

### 11. Gateway restart command

`hermes gateway restart`. From `hermes gateway --help`: `restart             Restart gateway
service`. The pack install prints the same command.

### 12. Registration signatures and hook kwargs

From `hermes_cli/plugins_context.py`:

- `:444` `register_cli_command(self, name: str, help: str, setup_fn: Callable, handler_fn: Callable | None = None, description: str = "") -> PluginRegistration`. `setup_fn` gets the argparse subparser; `handler_fn` becomes `set_defaults(func=...)`.
- `:458` `register_command(self, name: str, handler: Callable, description: str = "", args_hint: str = "", argument_mode: str | None = None) -> Optional[PluginRegistration]`. The handler is `fn(raw_args: str) -> str | None`, sync or async. A name that clashes with a built-in command is skipped with a warning. ponytail 5.3.3 calls it as `ctx.register_command("ponytail", handler, description=..., args_hint=...)`.
- `:753` `register_hook(self, hook_name: str, callback: Callable, *, hold_stream: bool = False) -> PluginRegistration`.
- `:75` `get_config(self, key: str, default: Any = None) -> Any`, described in finding 9.

Kwargs Hermes passed, from the probe log:

- `post_llm_call` (`agent/turn_finalizer.py:501`): `assistant_response`, `conversation_history`, `model`, `platform`, `review_signals`, `session_id`, `side_question_token`, `task_id`, `telemetry_schema_version`, `turn_id`, `user_message`.
- `on_session_end` (`agent/turn_finalizer.py:910-921`): `completed`, `failed`, `interrupted`, `model`, `platform`, `session_id`, `task_id`, `telemetry_schema_version`, `turn_exit_reason`, `turn_id`. It fires at the end of every turn; the comment there says `run_conversation()` runs once per message.

All hooks ran on `hermes-hook-callback`; `register` ran on `plugin-load:hook-probe`.

### 13. Keys per profile and `get_model_context_length("auto")`

Hermes loads `<HERMES_HOME>/.env` into `os.environ` at start
(`hermes_cli/env_loader.py:748`). In `create_client` both fake variables were in `os.environ`,
including `POLYTROPOS_SPIKE_OTHER`, which no profile declares
(`"key_in_os_environ": true, "other_key_in_os_environ": true`). The provider key itself is resolved
from the bound home's `.env` and the scoped environment (`hermes_cli/config.py:2687-2714`).
`os.environ` holds the launch profile's values, so in a gateway that serves several profiles it
can hold another profile's key. `hermes_cli.config.get_env_value(name)` is the scope-aware reader
(`hermes_cli/config.py:2687-2699`).

`get_model_context_length` is called with `"auto"`: 5 times in the flat run, 4 in the nested and
pack runs, and 3 in the run without a key, from `asyncio_*` threads and `hermes-gateway_1`.

## Other observations

**A one-shot run starts a gateway daemon.** `hermes -z` calls
`ensure_gateway_runtime(..., idle_exit=True)` (`hermes_cli/gateway_runtime.py:415-432`). For the
sandbox home it found no installed service (the label for a custom home is
`ai.hermes.gateway-<8 hex of the path>`, `hermes_cli/gateway_runtime_service.py:44-65`, `:204-235`)
and started `gateway run --quiet --idle-exit` for that home. The provider client and every hook ran
in that daemon (pid 16320 in the flat run). `gateway.pid` in the home names it; during the first
attempt it read:

```
{"pid": 14364, "kind": "hermes-gateway", "argv": [".../hermes_cli/main.py", "gateway", "run", "--quiet", "--idle-exit"], ..., "hermes_home": ".../.sandbox/turns/hermes"}
```

The daemon stays up for `gateway.unmanaged_idle_exit_seconds` (default 600,
`hermes_cli/config_defaults.py:2055`; log: `Auto-started gateway: exits after 600s with no clients,
adapters, cron/kanban work or admissions`) and serves the next run of the same home. In the first
attempt the daemon of the first run (pid 14364) also served the next three runs. It re-imported
the changed provider tree (new module suffix `b9963ce985df`), while its hook records still lacked
the `pid` field that the edited `hook_probe` writes, so the general plugin kept its old code. It
also still had the first run's `.env` values in `os.environ`, so the run without a `.env`
succeeded. `scripts/hermes_sandbox.stop_gateway(home)`
stops the daemon named in `gateway.pid` after checking that the file names this home and that the
live command line ends with the arguments the file records (a real gateway shows them after an
inline launcher, without `main.py`). It sends SIGTERM, then SIGKILL after a timeout, and removes the
pid file after a SIGKILL. During the spike the driver called it before and after each run; since
fix round 1, `run_oneshot` does that itself. The table above comes from runs with a fresh daemon
each. A daemon used about 150 MB of memory.

**On macOS that daemon is a one-off launchd job.** `start_gateway_job`
(`hermes_cli/gateway_launchd_runtime.py:100-129`) writes `<home>/logs/.com.nousresearch.hermes.runtime.<12 hex>.plist`,
runs `launchctl bootstrap gui/<uid>` with it and deletes the file. The job has `RunAtLoad` and no
`KeepAlive`, and the file is never in `~/Library/LaunchAgents`. After the gateway exits, the label
stays loaded until the next unmanaged gateway start of any home removes finished
`com.nousresearch.hermes.runtime.*` jobs (`:91-97`).

**A fresh home builds its own dependency environment.** The first run in a new `HERMES_HOME`
printed `hermes: preparing dependencies for this data root (the checkout's updates belong to
~/.hermes)...` and `Preparing the isolated Hermes runtime…`. It wrote about
500 MB under `<home>/installs/` and 320 MB of uv cache, and fetched the plugin catalog and
`models_dev_cache.json` over the internet. Later runs in the same home reuse it. The interpreter
comes from the maintainer's store in `~/.hermes/tools`. A `config.yaml` without `_config_version` logs
`config migration skipped: This config predates version 12`; the runs were not affected.

**A launch leases the environment it runs from.** The process creates a lock file in
`<root>/installs/<key>/environments/<id>/.leases/` and removes it at exit
(`hermes_cli/runtime_state.py:147-179`). The first `--help` runs, before the sandbox had its own
environment, leased the maintainer's environment: the mtime of
`~/.hermes/installs/<key>/environments/<id>/.leases` changed to 21:20:31. Its four lease files from
2026-11-18 are still there and no file was added.

**The gateway half of spec spike 1 is skipped.** The sandbox has no messaging platform, and the
maintainer's gateway stays untouched: `ai.hermes.gateway` kept its pid `<pid>`, started
2026-11-18 20:01, through the whole spike.

## Launchd difference

`launch_snapshot()` before the first Hermes run had 33 entries; after the last run it had 34. Added:
`com.nousresearch.hermes.runtime.<hex>`. Nothing was removed, and no file was added to or
removed from `~/Library/LaunchAgents`. `launchctl print gui/<uid>/com.nousresearch.hermes.runtime.<hex>`
shows `type = LaunchAgent`, `state = not running`, `program = /usr/bin/osascript`, the arguments
`... gateway run --quiet --idle-exit >> .../.sandbox/turns/hermes/logs/gateway-stdio.log ...` and
`path = .../.sandbox/turns/hermes/logs/.com.nousresearch.hermes.runtime.<hex>.plist`, a file
that no longer exists. It is the job of the last sandbox daemon. Its label ends in random hex
(`uuid4`, `hermes_cli/gateway_launchd_runtime.py:107`). The job is inert, and the next unmanaged
gateway start of any Hermes home removes finished jobs like it (`:91-97`). By the controller's
ruling it stays in place, and nobody runs `launchctl` on it.

So `test_no_launchd_changes` as first written fails on macOS after any `hermes -z` run. By the
controller's ruling Task 24 drops `com.nousresearch.hermes.runtime.*` and `application.*` labels
from both snapshots, checks that none of its own runtime jobs still runs at the end, and never
runs `launchctl remove` or `bootout`.

## Plan changes

Tasks 9, 10, 16, 17 and 22 in `docs/superpowers/plans/2026-11-19-polytropos.md` carry these
findings. Fix round 1 added the controller's rulings to Tasks 9, 10, 16, 17, 22, 23 and 24.
