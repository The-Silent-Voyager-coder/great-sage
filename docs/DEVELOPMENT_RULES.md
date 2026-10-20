# Development Rules

These rules bind every human contributor **and every AI agent** working in
this repository. They are the Phase 0 founding contract.

## 1. Anti-Patterns (FORBIDDEN)

1. **No wholesale rewrites** — never rewrite the entire project "to make it
   cleaner." Refactor incrementally with tests.
2. **No unjustified dependencies** — see `docs/DEPENDENCY_POLICY.md`. Adding a
   dependency without a written justification is a violation.
3. **No duplicate abstractions** — if an interface already exists, extend it.
   Do not invent a parallel one.
4. **No god files** — functionality is distributed across modules; a single
   file holding everything is rejected in review.
5. **No hard-coded API keys** — keys come from environment variables only
   (see `docs/CONFIGURATION.md` §4). A leaked key = incident.
6. **No hard-coded user-specific absolute paths** — `C:\Users\ASUS\...` never
   appears in code or config that ships; everything goes through `core.*`
   config keys with defaults under `C:\GREATSAGE\`.
7. **No silent security weakening** — never lower a risk level, widen a
   permission scope, or disable an approval "so the build passes."
8. **No test weakening** — never disable, skip, or mock-away a failing test to
   make a CI green. Fix the code or write the correct test.
9. **No false completion** — never mark a task COMPLETED while its
   verification steps are pending/failed. Evidence is required.
10. **No premature phases** — implement only what the current phase requires
    (see README phase table). Do not build vision UI during Phase 1.

## 2. When Uncertain

> Preserve the architecture and ask for clarification rather than inventing
> requirements.

If a spec point is ambiguous, conflicting, or would require inventing new
behavior: stop, state the ambiguity, propose options, wait. Do not improvise
a design that contradicts `docs/ARCHITECTURE.md` / `docs/INTERFACES.md`.

## 3. Architecture Boundaries

- `core` never depends on the OpenCode wire directly — only through the
  `AIProvider` abstraction (`docs/ARCHITECTURE.md` §4); the wire itself
  lives in `greatsage/intelligence/opencode.py` (the reserved
  `greatsage/integration/` package stays empty).
- Modules communicate via `events/` where decoupling is required; direct calls
  only for true service dependency.
- Permissions are enforced by `greatsage/tools/policy.py` (SecurityPolicy +
  hooks), never inlined into tools.

## 4. Code Standards (Phase 1+)

- Python 3.11+; type hints on all public functions; dataclasses for records.
- No comments that restate code; docstrings on public interfaces.
- Lint with ruff (config in `pyproject.toml`); format before commit.
- Every module change ships with its tests (`docs/TESTING.md`).
- Commit style: conventional (e.g. `feat(core): add service registry`,
  `fix(tools): scoped deny for delete outside root`).

## 5. OpenCode-Specific Rules for Any Agent Working Here

- Use OpenCode's server/API/SDK for integration — never screen scraping or
  UI automation (`docs/OPENCODE_INTEGRATION.md`).
- Treat OpenCode as an agent/tool: Great Sage remains responsible for
  understanding, planning, state, permissions, verification, reporting.
- Never assume `--auto`/auto-approval is safe for unrestricted operation
  (`docs/SECURITY_MODEL.md` §4).
- Use OpenCode's own granular permissions (`allow`/`ask`/`deny` per tool) as
  enforcement; do not bypass them.

## 6. Definition of Done (module-level)

A module is done when: feature works, tests pass (incl. safety subset), lint +
types clean, docs updated, audit/permission behavior verified, and the phase
gates from the README phase table are respected.