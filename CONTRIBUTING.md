# Contributing to OpenGCUA

OpenGCUA is a community project, and we welcome contributions from everyone. Whether you are a
developer, a researcher, or simply interested in computer-use agents, there are many ways to get involved.

## Feedback and suggestions

- Usage feedback on the harness and the SDK, and bug reports. Please attach the trajectory when reporting a
  failure, it makes the report actionable.
- Suggested research questions worth exploring for GCUA. Open an issue with the `research-question` label.
  A good issue states the question in one sentence and why an open stack is needed to answer it.

Questions we are already tracking, join these or add your own:

- What other harness decisions should move into the model, beyond `todo`, `intent`, and `screenshot`?
- When does programmatic control beat stepwise control, across environments and task types?
- Error recovery as a training objective.
- When should an agent ask the user instead of acting?
- Efficiency-normalized evaluation: turns, actions, tokens, and cost per task, comparable across labs.
- An open safety reviewer for computer use.

## Code contributions

- The harness and the SDK themselves.
- Platform support beyond macOS: Windows, Linux, browser, mobile.
- SDK bindings in languages other than Python.
- Driver improvements, for example element identity on macOS and multi-display support.
- Integrations with other agent frameworks. The SDK is model-agnostic by design, so running another model
  through it is itself a contribution.

Open a PR. Small, reviewable changes land fastest. For anything that touches the driver or the confirmation
gate, include a trajectory demonstrating the behavior.

## Evaluation

- Tasks and trajectories for real-device evaluation, especially on applications we have not covered.
- Replication of reported numbers under different configurations.
- Real-device runs on diverse hardware and macOS versions, and permission-flow reports.

Community results are featured in each numbered update.

## Ground rules

- Every reported number must ship with the harness configuration and step budget that produced it.
- No data distilled from closed models in any contribution intended for training.
- Label contributions honestly as released, preview, or planned, like we do.
