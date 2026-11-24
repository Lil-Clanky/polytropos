# Polymetis

Polymetis is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that decides per session which of Hermes's skills a model gets. On every turn it ranks the skills in Hermes's skill index against the user's message, on this machine and without a model, and it logs what it decided. By default it changes nothing that a model gets. Two features can be turned on: a short tip that names the matching skill, and a smaller request for local models behind `polytropos-router`.

It works on its own and needs no other part of Polytropos.

## Install

```sh
hermes plugins install Lil-Clanky/polytropos/plugins/polymetis
```

Hermes asks whether to enable it, and the default answer is no. Polymetis needs Hermes 0.23.2 or newer. Remove it with `hermes plugins remove polymetis`.

## What it does

- **Search.** Hermes lists every enabled skill in its system prompt, with a name, a category and a short description. Polymetis ranks these skills against the turn's message with BM25, a word-matching score, and keeps the 10 best. A word in a skill's name counts double. Nothing leaves the machine for this.
- **Level.** A session gets a level on its first turn that has a skill index to rank: `lean` when a skill reaches `min_score`, otherwise `none`. `full` comes only from `platform_levels`. The level stays for the session, so that a local model gets the same request on every turn and its prompt cache holds. A session that started at `none` because nothing matched moves to `lean` once, on the first later turn that matches a skill.
- **Log.** One line per turn with names, scores, counts and reasons, never message text.

With the default settings this is all it does. The setting `apply` turns on what it may change, and an ablation has keys of its own.

## Settings

Polymetis reads its settings from `plugins.entries.polymetis.settings` in the profile's `config.yaml` at the start of every turn, so a change counts from the next turn. Only the start of an ablation waits until Hermes loads the plugin again, see [Ablation](#ablation). Edit them with `hermes config edit`, for example:

```yaml
plugins:
  entries:
    polymetis:
      settings:
        apply: [tips]
        platform_levels:
          whatsapp: none
```

| Key | Default | Meaning |
|---|---|---|
| `apply` | `[]` | What Polymetis may change: `tips`, `local_tools`, or both. Empty means decide and log only, apart from an ablation. |
| `log` | `true` | Write the log and the last skill index per platform. With `false` Polymetis writes no files. |
| `min_score` | `5` | The score a skill needs for a tip and for a `lean` session, from 0 to 1000. The default comes from the offline measurement in `evals/polymetis`. |
| `max_tips` | `2` | The most skills one tip names, from 0 to 5 |
| `pin` | `[]` | Skills that always rank first, and that a `lean` request for a local model keeps when the index has them |
| `block` | `[]` | Skills that are never ranked or tipped |
| `platform_levels` | `{}` | A fixed level per gateway platform, such as `whatsapp: none` or `cli: full` |
| `local_tools` | `clarify`, `memory`, `read_file`, `search_files`, `terminal`, `web_search`, `write_file` | The tools a local model keeps at `lean` and `none` |
| `ablation.share` | `0` | The share of new sessions that run without skills, from 0 to 1. It starts when Hermes next loads the plugin, as after a restart of Hermes or the gateway. |
| `ablation.until` | none | The last day of the ablation, such as `2026-12-31`. Without it the ablation runs until `share` is 0 again. |

A wrong value keeps its default, and Hermes's log names the key without the value.

### Tips

With `apply: [tips]`, a turn whose best skill reaches `min_score` gets a hint such as

```
<skill-hint>Relevant skill: github. Load one with skill_view if you need it.</skill-hint>
```

The hint names at most `max_tips` skills, and only skills that score at least half of the best one. Polymetis returns it from Hermes's `pre_llm_call` hook. Hermes appends it to the user message of that turn, and later requests repeat it. The model decides whether to load the skill. A session at `none` gets no tips.

### Smaller requests for local models

With `apply: [local_tools]` and `polytropos-router` as the model provider, the router asks Polymetis for the request that a local model gets. Polymetis finds the session by the request's system prompt, first user message and last user message, as Hermes sent them in that turn, and returns one of four variants. Other requests, such as those of Hermes's background review, go to the model as they are.

| Variant | Tools | Skill index |
|---|---|---|
| `full` | all of Hermes's tools | Hermes's own |
| `lean` | `local_tools` and `skill_view` | only the session's skills: the pins, and the skills tipped on the turn that set the level |
| `none` | `local_tools` | none, and no sentences that point at `skill_view` |
| `bare` | none | none |

The variant starts at the session's level, or at `full` while the session has none. A `lean` session without skills of its own gets the same request as `none`. When a variant does not fit the local model's window by the router's own count, Polymetis takes the next one down, with `bare` as the last. Every variant except `full` also drops the tips from the user messages.

Only local models get these variants. More requests fit a local model this way, so more turns can run on one. The router's memory guard still checks every local call. Without `polytropos-router` this feature does nothing.

### Ablation

An ablation measures whether skills help. While `ablation.share` is above 0 and `ablation.until` has not passed, that share of new sessions, picked by a hash of the session id, runs without skills. An `llm_request` middleware removes the skill index, the sentences that point at `skill_view`, and the tools `skills_list`, `skill_view` and `skill_manage` from the requests of those sessions' turns. It reads the formats Hermes sends to chat completions, Anthropic, the Responses API and Bedrock; a request in another format keeps its skills. Hermes's `codex_app_server` mode sends no request through the middleware. The log marks these sessions with `arm: ablation` and the other new sessions with `arm: normal`, and the report compares both groups.

Only a session that starts while the ablation runs gets a group: a session is new when it has no history yet. One that Polymetis first sees on a later turn may have used its skills already, so it gets none. A session leaves its group, and has its skills from then on, when the ablation ends or when Polymetis loses track of it: after a restart of Hermes or the gateway, after Hermes compresses the conversation and gives it a new session id, or when it is not among the last 256 sessions that Polymetis saw. Its later turns, like those of every session without a group, have `arm: null`. The report compares both groups up to that point, so both cover the same part of their sessions.

While a plugin has an `llm_request` middleware, Hermes deep-copies every request several times, so Polymetis adds its middleware only when Hermes loads it while an ablation is set. After setting `ablation.share`, restart Hermes or the gateway; until Hermes loads the plugin again, its log says that the ablation waits for a restart. When the ablation ends, by `until` or a `share` of 0, its sessions get their skills back from their next turn.

## Files

Polymetis writes only in the data folder that Hermes gives it in the profile, `<HERMES_HOME>/plugin-data/agent-plugin-polymetis-1717164d/`, and only while `log` is on. The report ends with the log's path. Both files have mode 0600.

- `decisions.jsonl`: one line per turn, written when Hermes ends the turn with its `on_session_end` hook. Hermes's `codex_app_server` mode ends a turn without that hook, so there the log gets a line only for a turn in progress when the CLI or TUI closes. At 5 MB it moves to `decisions.jsonl.1`, which replaces the older one.
- `index-<platform>.json`: the names, categories and descriptions of the skills in the last index that Hermes sent on that platform. With it, the first turn of a new session can be ranked before Hermes sends that session's first request, also after a restart.

A line of `decisions.jsonl` has these fields.

| Field | Meaning |
|---|---|
| `time` | Unix time at the end of the turn |
| `session` | 12 hex characters of a SHA-256 of Hermes's session id |
| `turn` | The turn's number in the session, as Polymetis counted it |
| `platform`, `provider`, `model` | From Hermes's hooks |
| `answered_by` | The models that the provider reported as answering, at most 3 |
| `level`, `level_reason` | The session's level, `null` before it has one, and why: `match`, `no_match`, `upgrade`, `platform:<name>` or `ablation` |
| `arm` | The group of a session that started while an ablation ran, `normal` or `ablation`, until it leaves the group; otherwise `null` |
| `apply` | The features that were on |
| `offered_skills`, `offered_tools` | How many skills the index held, and how many tools the request had after any middleware. For a request that Hermes passes on only in short form, it is Hermes's count of the agent's tools. |
| `candidates` | The 5 best skills with their scores |
| `tip`, `tip_given` | The skills a tip would name, and whether it was sent |
| `local`, `chosen` | The variant prepared for a local model, and the skills a `lean` local request keeps |
| `used_skills`, `used_tools`, `tool_errors` | The skills from the index that the model loaded with `skill_view`; the tool calls per tool, where a name that does not look like a tool's counts as `other`; and how many calls failed |
| `api_calls`, `tokens_in`, `tokens_out` | Requests in the turn and the token counts the provider reported |
| `duration_ms`, `outcome` | The turn's length, and `completed`, `failed`, `interrupted` or Hermes's exit reason |
| `correction`, `repeat` | Whether the message starts like a correction, with words such as "no", "actually", "nee" or "klopt niet", and whether it and the message before share at least 70% of their words |

Polymetis keeps the text of the current and the previous message in memory only, for the search and these two fields. It makes no network calls. Hermes's background review, which looks back at a turn with the same session id, counts in no turn.

## Report

```sh
hermes polymetis report --days 30
```

The report reads the log and prints counts: session levels, turns per platform, how often the model loaded a tipped skill, the most loaded skills, the variants prepared for local models, outcomes, tool errors, corrections and repeats. It also makes proposals, and changes nothing itself:

- skills that no turn loaded, to leave out with Hermes's `skills.disabled`, once the log covers 14 days or more;
- an ablation week, when no ablation ran in the last 90 days and either a model answered for the first time in the last 7 days or the log goes back 90 days or more;
- during and after an ablation, a comparison of both groups per turn: tool errors, corrections, repeats, failed turns and tokens. It counts only the sessions that started while the ablation ran, up to when they left their group, and leaves out turns without a request that Polymetis saw.

## Measurements

- `docs/spikes/2026-11-24-skill-exposure.md`: how two small Ollama models react to a skill index without tools, and where Ollama puts tool definitions in the prompt.
- `evals/polymetis/README.md`: how often the search finds the right skill, in English and Dutch, over 44 to 185 skills.

## License

MIT, as the rest of this repository.
