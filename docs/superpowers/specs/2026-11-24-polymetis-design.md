# Polymetis: a skill router for Hermes, design

Date: 2026-11-24. Read against Hermes `main` 2506f5b0 and Polytropos `main` 9686acf.

## Goal

Decide per session which skills and tools a Hermes model gets: none, a short tip, or Hermes's full
default. Model and skills are one decision. A strong cloud model picks well from the full index and
needs at most a tip. A small local model (3B, 8K window) chokes on a long index and on tool-search
rounds, and needs a small fixed set. A simple turn needs no skill.

Polymetis is a plugin of its own. It works with any provider, does not import Polytropos code, and is
installed on its own, like `whatsapp-human-pace`. With `polytropos-router` it also shapes the
requests that the router sends to a local model, through a small hook that the router offers to any
plugin. Without the router that part does nothing.

## Prior art in the Hermes catalog

`skill-router` (xXLODXx), `hermes-skill-router` (mortang2410), `jev-skill-router` and
`typesafe-skill-router` already route skills. They pick a skill per turn, with embeddings, a rerank
server or a cloud router, and the second one removes Hermes's skill index from every request.
None of them looks at which model answers. Polymetis does: its level depends on the model, it
keeps a local model's request fixed per session for the prompt cache, it sends no message text
anywhere, and it starts in a shadow mode that only measures.

## Hermes facts this design rests on

- `pre_llm_call` runs once per turn with `session_id`, `turn_id`, `user_message`, `platform`,
  `model`, `is_first_turn`. A returned `{"context": ...}` is appended to the turn's user message and
  persisted with it, so the system prompt and the prompt cache stay intact (`agent/turn_context.py`).
- `pre_api_request` runs before every provider attempt with `session_id`, `turn_id`, `platform`,
  `model`, `provider`, `base_url`, `system_prompt`, `request_messages` and `request.body.tools`
  (`agent/turn_api_request.py:60-110`). Auxiliary calls (titles, compression) do not fire it.
- The skill index is the `<available_skills>` block of the system prompt, one line per skill,
  `- name: description`, with the description cut to `skills.index_desc_limit` (60 characters by
  default) and categories as headers (`agent/prompt_builder.py:1305-1366`). It is already filtered by
  `skills.disabled`, `skills.platform_disabled` and the frontmatter conditions.
- `post_api_request` carries the normalized response with the model that answered and the usage.
- `post_tool_call` carries `tool_name`, `args`, `session_id`, `turn_id` and `status`
  (`model_tools.py:708-722`). A `skill_view` call names the skill in `args.name`.
- `on_session_end` runs at the end of every `run_conversation`, that is after every turn, with
  `completed`, `failed`, `interrupted` and `turn_exit_reason` (`agent/turn_finalizer.py:912-925`).
- `llm_request` middleware may replace the provider kwargs and fails open
  (`website/docs/developer-guide/middleware.md`). Hermes still validates tool calls against the
  session's full tool list, so hiding a tool steers the model and does not secure anything.
- `ctx.get_config(key)` reads `plugins.entries.<plugin>.settings.<key>`.

Measured on 2026-11-24 (see `docs/spikes/2026-11-24-skill-exposure.md`): small Ollama models place
tool definitions after the system text and before the whole history (qwen2.5, qwen3, phi4-mini,
smollm2, granite3.3) or in the last user message (llama3.2, ministral-3). A different tool set
therefore recomputes the history for most models, so a local model's set must stay fixed per session.

## Levels

| Level | Strong model | Local model behind the router |
|---|---|---|
| `none` | no tip | core tools only, no skill index |
| `lean` | a tip naming the one or two best skills for the turn | core tools, `skill_view`, and an index of only the session's one or two skills |
| `full` | Hermes's default, with tips | Hermes's default request |

A local request that does not fit the model's window steps down one level at a time, to `bare` (no
tools, no index) as the last step. The router judges the fit with the estimate and reply reserve it
uses to pick a model, so both always agree.

## Deciding

Cheap first, all local, no extra model call:

1. **Ablation arm.** During an ablation a share of new sessions, picked by a hash of the session id,
   gets `none` and no skills at all (see Ablation).
2. **Platform level.** `platform_levels.<platform>` sets the level, such as `whatsapp: none`.
3. **Search.** BM25 over each skill's name, category and description, with the turn's user text as
   the query, gives up to 10 candidates. `pin` skills rank first and `block` skills never appear.
   When the best score reaches `min_score` the session becomes `lean`, otherwise `none`.

The level is sticky for the session, because the local request must stay fixed. One exception: a
`none` session moves to `lean` once, on the first turn whose best score reaches `min_score`. That
costs the local model one recomputation of its prompt.

Tips are per turn. A tip goes into the user message, which keeps the prompt cache, so each turn can
name the skills that fit that turn.

The original plan put a judge in the router's classifier call for unclear cases. Polymetis is a
plugin of its own and does not reach into the router's classifier, and a call of its own would send
the user's text once more. The judge is left out until the offline measurement shows that BM25 is
not good enough. Embeddings would be the next step then, because they also handle Dutch questions
about English skill descriptions, but they need an embedding model running beside Hermes on every
turn, so they would be an opt-in of their own.

## Applying

`apply` lists what Polymetis may change. Empty is the shadow mode: decide and log, change nothing.

- `tips`: `pre_llm_call` returns `<skill-hint>Relevant skills: github. Load one with skill_view if
  you need it.</skill-hint>` when the level is not `none` and a skill reaches `min_score`.
- `local_tools`: Polymetis registers a request shaper with `polytropos-router`. Before the router
  sends a request to a local model, it asks the shaper for that model's request. The shaper finds
  the session through a fingerprint of the request (the system prompt, the first user message and
  the last one), which it recorded in `pre_api_request` for the turn's own requests, and builds the
  session's variant:
  - keep only the tools in `local_tools` (plus `skill_view` at `lean`);
  - at `none` and `bare`, remove the skill index block and every sentence that points at
    `skill_view(`; at `lean`, keep the block with the session's skills only;
  - remove `<skill-hint>` blocks from user messages, so the local model does not see tips for skills
    it cannot load.
  The variant is deterministic per session, so the local model's prompt cache holds across turns.
  Requests without a known session, such as Hermes's auxiliary calls and its background review,
  stay as they are.

## Ablation

With `ablation.share` above 0 and before `ablation.until`, that share of new sessions runs without
skills: an `llm_request` middleware removes the skill index, the pointers to `skill_view` and the
skill tools (`skills_list`, `skill_view`, `skill_manage`) from the requests of the session's turns, in
the formats of chat completions, Anthropic, the Responses API and Bedrock. While an `llm_request`
middleware is registered, Hermes deep-copies every request several times, so Polymetis registers it
only when an ablation is set while Hermes loads the plugin; a new ablation starts when Hermes next
loads it, and an ended one gives its sessions their skills back from the next turn.

Only a session that starts while the ablation runs, with no history yet, gets a group, `ablation` or
`normal`, which the log records as `arm`; a subagent that `delegate_task` starts gets its parent's
group. A session leaves its group when the ablation ends or when Polymetis loses track of it: after a
restart, after a compression that gives it a new session id, or when it falls out of the 256 sessions
Polymetis keeps. The report compares both groups up to that point and leaves out turns without a
request that Polymetis saw. Counting a session first seen later, or a whole session that changed
group, would bias the comparison: such sessions are long ones, and they would fall into one group
only. One bias is left: a session with its skills reaches a compression sooner, so the normal group
can lose its late turns earlier, and the comparison may understate what leaving skills out saves.
The report proposes an ablation when a model it has not seen before shows up in the log, or 90 days
after the last one.

## Log

`decisions.jsonl` in the plugin's data folder (`<HERMES_HOME>/plugin-data/agent-plugin-polymetis-1717164d/`), mode 0600,
one line per turn, written at `on_session_end` (which Hermes's `codex_app_server` mode skips), rotated
to `decisions.jsonl.1` above 5 MB. It holds names, numbers and reasons,
never message text:

`time`, `session` (12 hex characters of a SHA-256 of the session id), `turn`, `platform`,
`provider`, `model`, `answered_by`, `level`, `level_reason`, `arm`, `apply`, `offered_skills`,
`offered_tools`, `candidates` (top 5 with scores), `tip`, `tip_given`, `local` (the variant prepared
for a local model), `chosen` (the session's skills for a `lean` local request), `used_skills`, `used_tools`, `tool_errors`, `api_calls`, `tokens_in`,
`tokens_out`, `duration_ms`, `outcome`, `correction`, `repeat`.

`correction` and `repeat` are computed in memory from the turn's text and the previous turn's
text: a reply that starts with "no", "actually", "nee", "klopt niet" and the like, or a message that
shares most of its words with the one before. Only the boolean is logged.

## Report

`hermes polymetis report [--days N]` reads the log and prints counts and proposals, never changes:
levels and tips per platform, how often a tipped skill was loaded, skills offered for N days that no
turn used ("disable with `skills.disabled`?"), turns where a local variant lacked the skill the turn
matched, and the ablation proposal or the comparison of a finished ablation. No self-learning: one
user's data is too thin for that.

## The router's part

Polytropos-router gains three things, all useful without Polymetis:

- `_usable()` returns why a local model was skipped (`guard`, `window`, `fit`), and the trace lists
  `skipped: [{slot, why}]`, `canary` demotions included.
- The trace adds `tokens_est` and `duration_ms` for the request the answering slot got, and
  `shaped` (the variant a shaper returned), and rotates above 5 MB.
- The hook point: before it estimates or sends a request to a local slot, the router calls every
  callable in `sys.modules["polytropos_router_hooks"].local_request` with the request and
  `{upstream, model, window, fits}`, where `fits(request)` says whether a request fits that slot.
  A callable returns `{"request": ..., "variant": ...}` or `None`; the first request returned is
  used. Errors are logged once and ignored.

## Privacy

No message text leaves the machine because of Polymetis, and none is written to disk. Polymetis
makes no network calls. The log holds skill and tool names, scores, counts and reasons.

## Evaluation

1. **Offline, now.** `evals/polymetis/`: 48 tasks in English and Dutch with the skills they need
   (or none), over Hermes's 44 bundled skills, with 0, 50 and 141 extra distractor skills from
   Hermes's optional skills. Measures recall@1/3/10, how often a no-skill task stays below
   `min_score`, and the effect of the distractors. A test keeps the numbers from falling.
2. **Live, after approval.** The method of Hermes's `evals/core_tool_deferral`: the same tasks,
   3 runs each, arms `full`, `none`, `tips` and `oracle` (a tip with the right skill), on a cloud
   model and a local model through the router. Measures success, turns, tokens, time, wrong skill
   loaded and the effect of distractors. Needs a sandboxed Hermes and free-model quota.

## Out of scope for now

The judge, embeddings, tags from `SKILL.md` (the index has none), and trimming tools for local models
that Hermes talks to directly without the router.
