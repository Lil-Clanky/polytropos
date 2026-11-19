# coffee-vram as a replacement for the fit check

Question: could the community plugin coffee-vram replace `guard.fits` later?

Sources: the Hermes plugin catalog entry (`plugin-catalog/coffee-vram.yaml`, version 0.1.4, pinned to commit 43294ca) and the repository README at github.com/jimmy77733/coffee-vram. I did not read its source code or run it.

## Findings

- Maintenance: the repository was created on 2026-11-08 and last pushed on 2026-11-11. It has no stars and one maintainer. There is too little history to judge how well it is maintained.
- License: MIT, the same as Polytropos.
- What it does: it checks weights plus KV cache plus a margin before each local Ollama call, queues local calls, unloads idle models to make room, and keeps loaded models warm. It runs as an `llm_execution` middleware and needs Hermes 0.22.0 or newer.
- Apple Silicon: the README describes NVIDIA GPU memory readings and says that without NVIDIA it falls back to the sizes Ollama reports. It describes unified memory only for an NVIDIA machine and offers a system RAM budget pool. It mentions neither macOS memory pressure nor swap, which the Polytropos guard reads.
- Network use: it polls the configured Ollama URL and NVIDIA telemetry in the background. Calls to cloud providers are not touched.
- Side effects: when a model does not fit, it holds the call for up to 600 s by default and unloads idle Ollama models by default, including models other programs loaded unless they are pinned. The Polytropos guard refuses instead and leaves other models alone.

## Conclusion

It does not replace `fits` today. Its fit check overlaps with ours, but the queue and eviction behaviour is a policy decision that Polytropos does not make, and the README does not cover Apple Silicon. Version 0.1.0 ships the built-in check. Revisit when coffee-vram documents Apple Silicon or when measurements show that our estimate is wrong.
