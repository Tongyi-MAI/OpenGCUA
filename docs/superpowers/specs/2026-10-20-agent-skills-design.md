# Agent Skills for the macOS harness

Date: 2026-10-20

## Goal and boundaries

Give the `mac_agent` reusable, on-demand task workflows without adding every workflow to its already large system prompt. A user can discover, explicitly select, create, and reload local Skills. The agent can select a relevant Skill from a compact catalog and load its full instructions only when needed. The same harness path must work in the terminal TUI, demo hub, and bundled macOS app. The GUI-only `gui_agent` is out of scope.

Skills are instructions and optional local reference files, not new permissions or SDK tools. A Skill is not user authorization for foreground takeover, sending, deletion, purchase, credential access, or other consequential actions; existing confirmation rules and the runtime's foreground gate remain in force. This does not make the persistent Python/Bash worker a security sandbox. The current task and explicit user constraints outrank Skill instructions. The first release does not install remote Skills, run bundled scripts automatically, add a general MCP client, or auto-discover Skills from the current working directory.

## Alternatives considered

1. Put all workflow text in the system prompt: simplest implementation, but adds tokens to every model request and makes the core prompt harder to maintain.
2. Let the agent read arbitrary `SKILL.md` files with Bash: little host code, but no reliable discovery, explicit user selection, provenance, size control, or resume behavior.
3. **Chosen:** a host-managed registry exposes metadata first and loads validated content on demand. It adds a small component but makes CLI, app packaging, diagnostics, and task scope explicit.

## Storage and format

Use a directory per Skill with an Agent-Skills-compatible `SKILL.md`. Required YAML front matter: `name` and `description`. The body contains the workflow and may link to relative files under the same Skill directory. No custom executable manifest or dependency installer is needed in v1. A starter template demonstrates a specific trigger, prerequisites, step-by-step workflow, verification, and failure/stop conditions.

Discovery roots are:

- Bundled: `packages/harness/skills/` in a source checkout; copied into the app's resources for bundled use.
- Personal: `~/Library/Application Support/Generalist/skills/`, shared by TUI and the macOS app.
- Explicit extras: optional absolute paths in `skills_dirs` configuration. They are never inferred from the current working directory.

Each root contains only immediate child Skill directories. Skill names must be unique across roots; collisions are reported instead of silently shadowed. Invalid metadata, unreadable files, out-of-root symlinks, or oversized content are skipped with a visible diagnostic; a bad Skill does not prevent the agent from starting. Bound the number and size of advertised Skills so a local directory cannot flood the prompt. Do not read reference files during discovery. Keep secrets out of Skill files and trajectory diagnostics.

## Discovery and model context

The static `MAI_UI_SYSTEM_PROMPT` gains only a short instruction explaining the Skill catalog and how to load a Skill. The host assembles a catalog from validated `name` and `description` values and supplies it as a labeled, lower-priority context item **before** the current task text. The catalog is not copied into the system prompt. The user can explicitly choose a Skill; otherwise the model decides from the catalog whether a Skill is relevant. No fuzzy host-side task matcher runs Skills automatically.

The runtime exposes a small host-controlled `skills` binding to the persistent Python worker. `skills.list()` returns the catalog; `skills.load(name)` validates and activates one Skill, then makes its full `SKILL.md` body visible on the next model turn as a separate, clearly sourced observation. The call does not execute the Skill's scripts or references. An agent may read a named relative reference through a bounded `skills.read(name, relative_path)` operation when the Skill asks for it. Paths must remain within the selected Skill directory. Missing or invalid names and paths return actionable errors rather than falling back to arbitrary file reads.

An explicit TUI selection is attached before the next submitted message's model request, so it does not require an extra model step. A model-selected Skill costs one deliberate load step. Both paths use the same registry and render a `skill_loaded` event in the TUI, demo hub, and app host. Loaded contents are treated as workflow data, not an authorization, and cannot override system/user rules.

## Task lifecycle, compaction, and resume

An active Skill belongs to a task, not the whole conversation. Starting a new task clears the prior task's active set; a user must explicitly select it again or let the agent choose it from the catalog. Repeated loading of the same unchanged Skill in one task is idempotent. Record the Skill name, source root, and content digest in the trajectory, without copying secrets into a new diagnostic field.

The catalog and active Skill body are supplied once at the appropriate task/load boundary, not duplicated in every request. Context construction reintroduces them if history truncation or LLM compaction would otherwise remove the only copy. The TUI `/resume` flow starts a **new** task after the next user message, so it does not reactivate the saved task's Skills. A command-line resume that continues the same unfinished task may reload active Skill content only if the file still matches its recorded digest; if it changed or vanished, show a warning and require explicit re-selection. Do not silently use a different revision. Existing Python-memory and SDK-handle resume limitations remain unchanged.

## User experience

The TUI provides `/skills` to list available Skills, `/skill <name>` to select one for the next submitted message, `/skill new <name>` to create a personal starter directory, and `/skills reload` to revalidate edited files. These are host commands, never sent as ordinary task text. Tab completion offers known Skill names. The composer shows the pending explicit selection and the conversation shows activation or validation errors. Commands entered during a running turn obey the existing safe-boundary queue: they neither interrupt the turn nor answer an agent question or permission request.

An explicit `$skill-name` marker in a task message is equivalent to selecting that Skill, and is removed from the model-facing task text only when it exactly matches a known name. Unknown markers remain plain text and produce a visible suggestion rather than loading an arbitrary path. Plain CLI mode and demo hub accept explicit markers and agent selection even without TUI slash commands. The macOS app uses the shared registry; a dedicated Skill picker in the native UI is not required for v1. Documentation shows directory locations, the template, reload behavior, precedence, and the instruction-versus-permission boundary.

## Packaging and failure behavior

The app build copies bundled Skill directories into signed app resources; it never writes custom Skills into the app bundle. The user directory remains outside the bundle and writable by the user. The runtime resolves the bundled root from its actual installed resource location rather than assuming a source checkout. The source checkout and bundled app must discover the same built-ins in tests.

Malformed metadata, duplicate names, unreadable references, permission errors, stale digests, and prompt-size limits surface concise, source-specific diagnostics. They do not trigger a fallback to Bash or an automatic Skill download. Existing foreground consent and risky-operation review stay enforced by the host, independent of Skill wording.

## Verification

Unit tests cover discovery roots, metadata validation, duplicate names, size caps, path traversal and symlinks, reload, explicit selection, and missing or changed Skills. Context tests assert catalog-before-task order, no full-body preloading, one-time activation, preservation across compaction/history cutoff, and digest-safe resume. TUI tests cover slash completion, queued commands, pending selection, and `$name` parsing. Integration tests exercise a built-in Skill through `mac_agent`, verify no GUI-only change, and inspect the app bundle to confirm built-ins are included. README and QUICKSTART include a minimal authoring example and explain that Skill content cannot grant permissions.
