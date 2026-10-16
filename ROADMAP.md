# Roadmap

This file is the authoritative status table. The README summarizes it, this file is updated first.
We do not promise dates. Versions ship when the components are ready, and numbered updates in between
keep the interval from going silent.

## Release principles

1. **Open at every stage.** A fully open computer-use stack means weights, harness, SDK, trajectories,
   and benchmark. Every report marks each component as released, preview, or planned.
2. **Honest labeling.** Every number ships with the harness configuration and step budget that produced it,
   together with its trajectories. Where a component does not yet work, we say so and report why.
3. **No distillation from closed models.** Our model is trained on our own data, self-distillation from our
   larger models, and openly licensed sources. This limits what we can do quickly. It is also what makes the
   project open in more than name.

## Schedule

| Version | Model | Harness and SDK | Data and benchmark |
|---|---|---|---|
| **v0.1** (this preview) | OSWorld-v2-compatible evaluation code | Five-layer architecture, Qwen-Intelligence SDK (Python, macOS and Android adapters) | Demo scaffolding |
| **v0.2** (next) | **Weights released** | Complete macOS action layer, confirmation gating, AX-tree diff on macOS, verification primitives | Cleaned evaluation suite, representative trajectories, real-device benchmark preview |
| **v1.0** (later) | Agentic-reasoning improvements, advisor calls for small models, error-recovery training | Windows adapter, browser backend, multi-display, safety reviewer and policy | Full real-device benchmark, adversarial safety evaluation, cross-model harness results |

Held back from v0.1 by design and scheduled above: agentic-reasoning improvements, and advisor calls that
let a small model consult a larger one at hard steps.

## Component status

| Component | Status | Notes |
|---|---|---|
| Code-execution harness (OSWorld-v2) | 🟡 preview | evaluation scripts are being cleaned for release |
| `todo` / `intent` / `screenshot` control | 🟡 preview | present in the evaluation preview |
| Example trajectories | ⚪ planned | representative trajectories will ship with the evaluation suite |
| Real-device harness (macOS) | 🟡 preview | action layer incomplete, see `packages/harness/README.md` |
| Qwen-Intelligence SDK | 🟡 preview | Python, macOS and Android adapters |
| Model weights | ⚪ planned | next release |
| Reproducible macOS environments | ⚪ planned | for evaluation and rollout |
| Real-device benchmark | ⚪ planned | preview in the next release |

## How we report progress

Each version carries a changelog and this status table. Between versions we publish numbered updates:
what changed, what we learned, and what the community built. When trajectories are released, their
format and licensing will be documented alongside them.
