# Real-Device Harness — 🟡 preview

The harness that runs a GCUA on a user's own machine (macOS first). Benchmarks hold the world still;
a real machine does not: windows overlap, animations play, notifications arrive, permissions go through
the OS's own dialogs, and the agent must *find* its target before it can act on it.

## Five-layer architecture

Each layer has one responsibility; each can be replaced without changing the others.

| Layer | Responsibility | Where |
|---|---|---|
| **Model** | plans, chooses actions & targets, judges effects, decides when to observe / what to keep / when to ask | (model release: v0.2) |
| **Runtime & host** | agent loop; turns model text into calls; keeps interpreter state alive between turns; assembles context; handles confirmations, questions, interruptions | [`runtime/`](runtime/) |
| **Device SDK** | the vocabulary the model programs against; enforces "only act on what you have been shown" | [`../sdk/`](../sdk/) |
| **Driver** | above the OS's raw AX/screen APIs: durable element identities, versioned snapshots, filtered actionable elements, native mechanism per action, background execution | [`../sdk/adapters/macos/`](../sdk/adapters/macos/) |
| **Real applications & OS** | the source of truth — the one layer that cannot be simulated | — |

## Status in this preview

- ✅ architecture stable; agent loop, context assembly, trajectory recording
- ✅ macOS accessibility observation + background action delivery (first version)
- 🚧 confirmation gating (v0.2) — until then, run only on machines whose state you can afford to lose
- 🚧 AX-tree diff on macOS (v0.2)
- 🚧 multi-display (v1.0)

## Layout

```
packages/harness/
├── runtime/   agent loop, context assembly, turn boundaries, confirmation flow, recording/resume
├── configs/   runnable configurations
└── examples/
    └── osworld/   reference screenshot-action and code-action agents
```

The system prompt is defined in [`runtime/mac_agent.py`](runtime/mac_agent.py)
as `MAI_UI_SYSTEM_PROMPT`. The native macOS provider lives in
[`../sdk/adapters/macos/`](../sdk/adapters/macos/).
