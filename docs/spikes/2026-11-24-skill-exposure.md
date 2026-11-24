# Small local models, tool sets and the skill index

Question: before Polymetis makes requests smaller for local models, how do small Ollama models react when the tool set changes between turns (V1), when a request carries the skill index but no tools (V2), and when it carries the index but not `skill_view` (V3)?

Sources: the chat templates of seven models in the Ollama registry (V1), and `spikes/skill_exposure_probe.py` against a local Ollama on 2026-11-24 (V2, V3).

## V1: where the tools go in the prompt

| Model | Place of the tool definitions |
|---|---|
| qwen2.5, qwen3, smollm2, phi4-mini | In the system block, after the system text, before all messages |
| granite3.3 | A role of its own (`available_tools`) after the system block, before the messages |
| llama3.2 | In the last user message; the system block gains one sentence when tools are present |
| ministral-3 | In the last user message (`[AVAILABLE_TOOLS]`) |

No model puts the tools before the system text. In five of the seven they come before the whole history, so a different tool set keeps the cached system text but recomputes the history. For llama3.2, tools on or off changes the system block and recomputes everything. A local model's tool set must therefore stay the same for the whole session.

## V2 and V3: the probe

Setup: llama3.2:3b and qwen2.5:7b, `num_ctx` 8192, temperature 0.7, 8 prompts, 3 runs each, 4 conditions, 192 requests in all. The index lists Hermes's 44 bundled skills in Hermes's wording (`agent/prompt_builder.py` at commit 2506f5b0). The tools are short versions of `terminal`, `read_file`, `web_search` and `skill_view`.

- `control`: the index and all four tools.
- `no_tools`: the index, no tools (V2).
- `index_no_skill_tool`: the index and three tools, no `skill_view` (V3).
- `bare`: no index, no tools.

Counts are out of 24 requests per row. "Call as text" means the reply text holds something shaped like a tool call.

| Model | Condition | Tool call | `skill_view` call | Unknown tool | Call as text | Names `skill_view` | Prompt tokens |
|---|---|---|---|---|---|---|---|
| llama3.2:3b | control | 22 | 5 | 0 | 2 | 1 | 1282 |
| llama3.2:3b | no_tools | 0 | 0 | 0 | 8 | 9 | 1008 |
| llama3.2:3b | index_no_skill_tool | 21 | 0 | 0 | 3 | 0 | 1234 |
| llama3.2:3b | bare | 0 | 0 | 0 | 1 | 0 | 62 |
| qwen2.5:7b | control | 18 | 7 | 0 | 0 | 0 | 1269 |
| qwen2.5:7b | no_tools | 0 | 0 | 0 | 5 | 5 | 1008 |
| qwen2.5:7b | index_no_skill_tool | 7 | 0 | 0 | 0 | 0 | 1223 |
| qwen2.5:7b | bare | 0 | 0 | 0 | 0 | 0 | 54 |

## Findings

- V2: with the index and no tools, both models wrote tool calls as text (8 and 5 of 24) and named `skill_view` in their answer (9 and 5 of 24). Without the index that nearly disappears (1 and 0). Taking the tools away and leaving the index makes replies worse, so a request without tools must also lose the index.
- V3: neither model called `skill_view` when it was not offered: 0 calls to an unknown tool in 48 requests. I had expected such calls before the probe. qwen2.5 did change, though: it made a tool call in 7 of 24 requests against 18 with `skill_view`, and stopped using tools for the PDF merge, the arXiv search and the Python crash. llama3.2 hardly changed (21 against 22). The index and `skill_view` therefore go together.
- Cost: the index of 44 skills takes about 950 prompt tokens (`no_tools` against `bare`), about 21 tokens per skill. At that rate the 185 skills that Hermes can install would take about 4,000 tokens, half of an 8K window. That figure is an extrapolation, not a measurement.
- In one llama3.2 run (`index_no_skill_tool`, the pull request prompt) a single reply asked for 17 terminal calls.

## Limits

Two models, 24 requests per row, one temperature, Ollama only. The probe looks at the shape of a reply, not at its quality. Time per request is not compared: a reply without tools is long text, a reply with tools is a short call.

## Conclusion

Polymetis gives a local model one fixed request variant per session. `lean` keeps the index (of the session's skills only) together with `skill_view`; `none` removes both; `bare` has no tools and no index. These rules are in `plugins/polymetis/shape.py`.
