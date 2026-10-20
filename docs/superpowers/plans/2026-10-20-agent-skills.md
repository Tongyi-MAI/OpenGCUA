# Agent Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add discoverable, on-demand `SKILL.md` workflows to `mac_agent` across TUI, demo hub, and the bundled macOS app, without changing `gui_agent` or granting new permissions.

**Architecture:** `skill_registry.py` owns discovery, validation, templates, and bounded reads. `skill_context.py` owns task-scoped activation and model-context reconstruction. The parent harness provides explicit selection and records activation; the Python worker exposes `skills.list/load/read` and reports loads through its existing execution events.

**Tech Stack:** Python 3.11+, existing argparse / prompt-toolkit / pytest; Markdown `SKILL.md` with a constrained YAML-front-matter subset; Swift app bundle built by `packages/macos/scripts/build.py`.

**Spec:** `docs/superpowers/specs/2026-10-20-agent-skills-design.md`

## Global Constraints

- `gui_agent` is out of scope; no general MCP client or remote Skill installer.
- Bundled root: `packages/harness/skills/` in source and `Contents/Resources/skills/` in the app.
- Personal root: `~/Library/Application Support/Generalist/skills/`; `skills_dirs` adds only explicit absolute roots. Never scan the current working directory.
- Skill instructions do not authorize foreground or consequential actions; the existing GUI gate and user-confirmation rules remain intact.
- Never auto-run bundled scripts, install dependencies, or silently shadow a duplicate name.
- Keep catalog metadata outside the system prompt and load the Skill body only on selection.

## Review Focus

1. Two roots defining the same name must yield a visible conflict, not nondeterministic precedence (Task 1 test).
2. A symlinked Skill or reference escaping its root must be rejected before file content is read (Task 1 test).
3. Malformed or very large front matter must not prevent startup or flood the model context (Task 1 test).
4. A Skill changed between trajectory save and CLI resume must not silently become the active revision (Task 3 test).
5. `/skill` typed during a permission/question prompt must not answer that prompt or activate mid-action (Task 4 test).

---

### Task 1: Registry, validation, and authoring template

**Files:**
- Create: `packages/harness/runtime/skill_registry.py`
- Create: `packages/harness/runtime/tests/test_skill_registry.py`
- Modify: `packages/harness/runtime/pyproject.toml`
- Modify: `packages/harness/runtime/mac_agent.py` (add `--skills-dirs` config destination)
- Modify: `packages/harness/configs/macos_demo.json` (default `"skills_dirs": []`)

**Interfaces:**
- Produces: `SkillRegistry(bundled_root: Path, personal_root: Path, extra_roots: Sequence[Path] = ())` with `discover() -> tuple[SkillInfo, ...]`, `load(name: str) -> SkillDocument`, `read(name: str, relative_path: str) -> str`, `create(name: str) -> Path`, and `diagnostics: tuple[str, ...]`.
- Produces: immutable `SkillInfo(name, description, path, source, digest)` and `SkillDocument(info, body)`; `parse_explicit_skill(message: str, known_names: Collection[str]) -> tuple[str, tuple[str, ...]]`.

- [ ] **Step 1: Write failing registry tests.** Cover a valid built-in and personal Skill, exact-name marker parsing, duplicate-name rejection, malformed/oversized metadata, relative-path and symlink escape, explicit extra root, and `create` refusing an existing directory. Assert `discover()` never scans a current-working-directory Skill.
- [ ] **Step 2: Run the focused tests to confirm red.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_registry.py -q`; expect failures because the registry is absent.
- [ ] **Step 3: Implement the registry and template.** Parse only scalar `name` and `description` front matter in v1, with clear errors for unsupported YAML forms; use UTF-8, a strict kebab-case name, maximum 64 Skills, 240 description characters, 64 KiB `SKILL.md`, 256 KiB text reference, and 16 KiB catalog. Resolve every file path and enforce containment. Keep discovery read-only; `create` writes only into the personal root. Add `skills_dirs` config parsing without adding a new package dependency.
- [ ] **Step 4: Run focused and configuration tests.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_registry.py packages/harness/runtime/tests/test_sdk_runtime.py -q`; expect pass.
- [ ] **Step 5: Commit.** `git add packages/harness/runtime/skill_registry.py packages/harness/runtime/tests/test_skill_registry.py packages/harness/runtime/pyproject.toml packages/harness/runtime/mac_agent.py packages/harness/configs/macos_demo.json && git commit -m "feat: add local skill registry"`.

### Task 2: Worker-side load/read and visible events

**Files:**
- Create: `packages/harness/runtime/skill_runtime.py`
- Modify: `packages/harness/runtime/code_runtime.py`
- Modify: `packages/harness/runtime/code_executor.py`
- Modify: `packages/harness/runtime/pyproject.toml`
- Create: `packages/harness/runtime/tests/test_skill_runtime.py`

**Interfaces:**
- Consumes: Task 1 `SkillRegistry`, `SkillDocument`.
- Produces: `SkillRuntime(registry: SkillRegistry, emit: Callable[[dict[str, Any]], None])` with `list() -> tuple[SkillInfo, ...]`, `load(name: str) -> SkillDocument`, `read(name: str, relative_path: str) -> str`.
- `CodeExecutor.__init__()` gains `skill_roots: tuple[Path, ...]`; its worker config serializes these as `list[str]`. A load emits one `{"type": "skill_loaded", "name": ..., "digest": ..., "source": ...}` event and displays the selected body for the next model observation.

- [ ] **Step 1: Write failing worker tests.** Assert `skills` is present in the persistent namespace, a valid load returns the body and one event, repeat load is idempotent, a reference read stays within the Skill directory, and invalid names never cause fallback shell/file execution.
- [ ] **Step 2: Confirm red.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_runtime.py -q`; expect missing binding / event failures.
- [ ] **Step 3: Implement the worker bridge.** Pass validated roots through `CodeExecutor._config()`, create `SkillRuntime` at worker bootstrap, and forward structured `skill_loaded` through the existing execution-event path. Do not add methods to the mai-ui SDK or alter `gui_agent`.
- [ ] **Step 4: Confirm green.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_runtime.py packages/harness/runtime/tests/test_sdk_runtime.py -q`; expect pass.
- [ ] **Step 5: Commit.** `git add packages/harness/runtime/skill_runtime.py packages/harness/runtime/code_runtime.py packages/harness/runtime/code_executor.py packages/harness/runtime/pyproject.toml packages/harness/runtime/tests/test_skill_runtime.py && git commit -m "feat: expose skills in code worker"`.

### Task 3: Task-scoped model context, compaction, and resume

**Files:**
- Create: `packages/harness/runtime/skill_context.py`
- Modify: `packages/harness/runtime/mac_agent.py`
- Modify: `packages/harness/runtime/pyproject.toml`
- Create: `packages/harness/runtime/tests/test_skill_context.py`
- Modify: `packages/harness/runtime/tests/test_agent_frontend.py`
- Modify: `packages/harness/runtime/tests/test_demo_hub.py`
- Modify: `packages/harness/runtime/tests/test_native_host.py`

**Interfaces:**
- Consumes: Task 1 `SkillRegistry`, Task 2 `skill_loaded` event.
- Produces: `SkillContext(registry: SkillRegistry)` with `start_task(task_index: int)`, `activate(name: str, expected_digest: str | None = None)`, `catalog_message() -> dict[str, Any]`, `active_messages() -> list[dict[str, Any]]`, and `records() -> list[dict[str, str]]`.
- `ContextManager.__init__()` gains `skill_context: SkillContext | None = None`. Its `build_messages()` inserts a labeled catalog before the current task; activation-turn indices determine whether active Skill text needs reintroduction after history truncation or compaction. `TrajectoryWriter` records name/source/digest, not a new plaintext Skill copy.

- [ ] **Step 1: Write failing context tests.** Assert only metadata appears before activation; a loaded body appears as a separate user-role reference before the next model action; a new task clears active Skills; truncation/compaction reintroduces the selected body once; unchanged CLI resume may reload but changed/missing content warns; TUI `/resume` starts a new task without reactivation. Assert `skill_loaded` reaches demo hub and native host event consumers.
- [ ] **Step 2: Confirm red.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_context.py -q`; expect missing context behavior.
- [ ] **Step 3: Implement context integration.** Add a short Skill discovery rule to `MAI_UI_SYSTEM_PROMPT`; route worker load events to `SkillContext`; serialize digest records in trajectory; load from validated roots at `run_agent` startup; keep skill reference text lower priority than system and current user instructions.
- [ ] **Step 4: Confirm green and no existing context regression.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_context.py packages/harness/runtime/tests/test_agent_frontend.py packages/harness/runtime/tests/test_demo_hub.py packages/harness/runtime/tests/test_native_host.py packages/harness/runtime/tests/test_sdk_runtime.py -q`; expect pass.
- [ ] **Step 5: Commit.** `git add packages/harness/runtime/skill_context.py packages/harness/runtime/mac_agent.py packages/harness/runtime/pyproject.toml packages/harness/runtime/tests/test_skill_context.py packages/harness/runtime/tests/test_agent_frontend.py packages/harness/runtime/tests/test_demo_hub.py packages/harness/runtime/tests/test_native_host.py && git commit -m "feat: load skills into agent context on demand"`.

### Task 4: Explicit selection and TUI authoring commands

**Files:**
- Modify: `packages/harness/runtime/agent_events.py`
- Modify: `packages/harness/runtime/terminal_tui.py`
- Modify: `packages/harness/runtime/mac_agent.py`
- Modify: `packages/harness/runtime/tests/test_terminal_tui.py`
- Modify: `packages/harness/runtime/tests/test_agent_frontend.py`

**Interfaces:**
- Consumes: Task 1 marker parser/create/reload and Task 3 `SkillContext.activate()`.
- Produces: `TerminalCommand.kind` values `skills_list`, `skills_reload`, `skill_select`, `skill_create` with existing `target`; no Skill command enters ordinary user-message history.

- [ ] **Step 1: Write failing TUI and host tests.** Cover `/skills`, `/skills reload`, `/skill <name>`, `/skill new <name>`, tab completion, queued command ordering, a pending explicit selection before the next submitted message, `$name` in plain/demo input, unknown marker behavior, and no command consumption by question/approval prompts.
- [ ] **Step 2: Confirm red.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_terminal_tui.py packages/harness/runtime/tests/test_agent_frontend.py -q`; expect newly added tests to fail.
- [ ] **Step 3: Implement host-side commands.** Route through `read_task_boundary()` and the existing `TerminalInteraction` queue. Use `SkillRegistry.create()` only for `/skill new`; refresh completions from a host `skills_catalog` event. Strip exact known `$name` markers only at task-input boundaries, preserving the original visible user text and showing an unknown-name diagnostic.
- [ ] **Step 4: Confirm green.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_terminal_tui.py packages/harness/runtime/tests/test_agent_frontend.py -q`; expect pass.
- [ ] **Step 5: Commit.** `git add packages/harness/runtime/agent_events.py packages/harness/runtime/terminal_tui.py packages/harness/runtime/mac_agent.py packages/harness/runtime/tests/test_terminal_tui.py packages/harness/runtime/tests/test_agent_frontend.py && git commit -m "feat: add TUI skill commands and explicit selection"`.

### Task 5: Bundled example, app packaging, docs, and end-to-end verification

**Files:**
- Create: `packages/harness/skills/pdf-form/SKILL.md`
- Modify: `packages/harness/runtime/mac_agent.py` (replace the long PDF-specific prompt recipe with a short Skill pointer)
- Modify: `packages/macos/scripts/build.py`
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Create: `packages/harness/runtime/tests/test_skill_packaging.py`

**Interfaces:**
- Consumes: Tasks 1-4 registry and model/UI behaviors.
- Produces: bundled `pdf-form` Skill discoverable in source and app layout, plus an authoring quickstart.

- [ ] **Step 1: Write failing packaging and prompt tests.** Assert source/app resource resolution both find `pdf-form`, the app build copies the directory without writing personal Skills into the bundle, and the main prompt points to Skill loading instead of embedding the full PDF recipe.
- [ ] **Step 2: Confirm red.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests/test_skill_packaging.py -q`; expect missing bundle/example failures.
- [ ] **Step 3: Add example and packaging/docs.** Move only the PDF-specific workflow to `pdf-form/SKILL.md`; keep global safety/AX/coordinate rules in the base prompt. Copy bundled Skills into `Contents/Resources/skills`; document `/skills`, `/skill`, `$name`, `skills_dirs`, editing/reloading, and the fact that Skills cannot authorize actions or auto-run scripts.
- [ ] **Step 4: Run the full offline suite and app-package smoke check.** Run: `.venv/bin/python -m pytest packages/harness/runtime/tests -q`; expect all tests pass. Run the focused app layout test in `test_skill_packaging.py`; expect the built-in Skill present at the path used by the bundled runtime. If a full signed app build is available, run it and report separately; do not claim one from a layout test.
- [ ] **Step 5: Review diff and commit.** Run `git diff --check`, inspect the exact changed files, then `git add packages/harness/skills/pdf-form/SKILL.md packages/harness/runtime/mac_agent.py packages/macos/scripts/build.py README.md QUICKSTART.md packages/harness/runtime/tests/test_skill_packaging.py && git commit -m "feat: bundle and document agent skills"`.

## Final verification

- [ ] Run the complete runtime test suite fresh and report counts, skips, and any failures.
- [ ] Exercise one explicit-selection task and one model-selected Skill with a fake model/worker; assert the Skill body is not in the first request unless explicitly selected, and the trajectory records the selected digest.
- [ ] Check `git diff --check`, final working-tree status, and the app resource path. State clearly whether a signed app build was actually run.
- [ ] Revisit the spec's out-of-scope items: no `gui_agent` change, MCP integration, remote Skill install, or automatic script execution.
