# Polytropos router

This plugin is a model provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent), called `polytropos-router`, with one model, `auto`. A session runs on `auto`, and the router chooses a real model for each turn. A short classifier call puts the turn on the `simple` or the `complex` route, and each route is a list of models that the router tries in order. A local model only gets the turn when the machine has memory to spare and the request fits the `context_window` set for it. Otherwise the turn goes to free cloud models through [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi).

The router is set up by the [polytropos](../polytropos/README.md) plugin.

## Install

```sh
hermes plugins install polytropos
hermes plugins install polytropos-router
hermes polytropos doctor
```

Then follow the steps in the [polytropos plugin](../polytropos/README.md#install). `hermes polytropos setup` writes the router settings, and the doctor can ask whether to make the router the default model.

Hermes needs `FREELLMAPI_API_KEY` for every turn on the router, also when all routes are local.

## What it sends and where

- Each turn goes to the models on its routes. By default those are FreeLLMAPI and Ollama on this machine. You can add any other OpenAI-compatible URL.
- The classifier sends at most 2,000 characters of the last user message to the classifier model, which is FreeLLMAPI's `auto` model by default. It does so also when the turn then runs on a local model.
- It asks the Ollama API for its version and for the models in memory.
- It downloads stack-canary's public test results from raw.githubusercontent.com at most once a day.
- Traces are off by default. When you turn them on, they record the route and the model of each request without message text.
- Upstream keys are read through Hermes's secret scope. The router runs no shell commands and sends no usage data anywhere.

Every setting is in the [reference](../../docs/reference.md).

## License

MIT, see [LICENSE](../../LICENSE).
