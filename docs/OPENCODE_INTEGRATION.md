# OpenCode Integration Strategy

> Verified against the OpenCode v2 OpenAPI spec (`GET /openapi.json`,
> server v2.0.29) plus live probes. OpenCode exposes
> a **headless HTTP server** ("`opencode serve`") with an OpenAPI 3.1 spec —
> Great Sage integrates through this supported surface only. **Never** screen
> scrape, click the UI, parse terminal screenshots, or fake human input.

## 1. Server Surface

```text
opencode serve [--port 4096] [--hostname 127.0.0.1] [--cors <origin>]
```

| Item | Value |
|---|---|
| Default endpoint | `http://127.0.0.1:4096` + `/api` prefix (config-overridable) |
| Spec | `GET /openapi.json` (server root) — OpenAPI 3.1 (use to generate clients / validate schemas) |
| Health | `GET /api/info` → `{version, pid, urls, paths}` |
| Events | Global `GET /api/event` SSE bus exists, but the provider polls per-session state instead: `GET /api/session/:id/message` (timeline, polled until an `idle` message lands) + `GET /api/session/:id/permission` (pending approvals). Deterministic, bounded, no long-lived stream to babysit. |
| Auth (required) | HTTP Basic `username:password` — username `opencode` unless overridden, password from env (`OPENCODE_SERVER_PASSWORD` convention; configured via `api_key_env` / `username_env`, never committed) |

## 2. Client Architecture (`greatsage/integration/opencode/`)

```text
OpenCodeProvider (v2 API under /api, HTTP Basic auth from env)
├── health()            → GET /api/info (READY iff a version string is present)
├── spec()              → GET /openapi.json (best-effort metadata only)
├── create_session()    → POST /api/session {title?, model?{providerID, id, variant?}}
├── prompt+wait         → POST /api/session/:id/prompt {text} (async inbox),
│                         then poll GET /api/session/:id/message until idle
├── list_permissions()  → GET /api/session/:id/permission (pending approvals)
├── respond_permission(id, requestID, approved, remember?)
│                       → POST /api/session/:id/permission/:requestID/reply
│                         {decision: once|always|reject}
├── abort_session(id)   → POST /api/session/:id/interrupt
├── session_diff(id)    → GET /api/session/:id/diff → {files: [...]}
└── dispose_session(id) → DELETE /api/session/:id (204-tolerant) + drop poll state
```

Implementation notes:

- Generate a typed client from `/openapi.json` at development time (or commit a
  vendored model of the relevant endpoints); do **not** hand-maintain
  ad-hoc JSON parsing per endpoint.
- All transport details (base URL, auth, timeouts) are config-driven.
- Executor progress is read with bounded **poll sweeps** (message timeline +
  pending permissions) and mapped to `DelegationEvent`s; the manager applies
  its deadline/reconnect policy and a 2s pause between sweeps, so the
  executor is never hammered back-to-back.

## 3. Delegation Workflow

```text
1. Detect: task needs substantial coding  (planner heuristic: multi-file edits,
   build/test cycles, project-level changes)
2. Identify workspace: existing project under C:\GREATSAGE\workspaces\ or new repo
3. Create OpenCode session (v2 API: POST /api/session)
4. Provide the prompt: POST /api/session/:id/prompt {text} — one message
5. Monitor: poll /api/session/:id/message (until idle) +
   /api/session/:id/permission; task state driven by translated events
6. Receive progress events (assistant turns, permission requests, failures)
7. Inspect results: read the assistant content + GET /api/session/:id/diff?from=
8. Verify locally: run tests/builds/static checks ourselves (Phase 4 tools;
   Phase 7 verification) — do not trust the agent's self-report
9. Request more work: send a follow-up prompt in the same session
10. Determine completion: completion_criteria validated by Great Sage, not OpenCode
11. Report: summary + evidence (diffs, test output) to user
```

## 4. Permission Handling (critical)

OpenCode enforces granular permissions (`read`, `edit`, `bash`, `task`,
`websearch`, `webfetch`, `external_directory`, `skill`, `question`, …) with
`allow` / `ask` / `deny`. Great Sage **uses** this machinery; it never
bypasses it:

- Permission requests surface in the poll sweep
  (`GET /api/session/:id/permission`) and are answered via
  `POST /api/session/:id/permission/:requestID/reply` with a Great Sage
  Security Layer decision:
  - decision = ALLOW → respond `{decision: "once"}` (or `"always"` when
    the operator grants it for the session)
  - decision = ASK → prompt the user with the exact action + risk level
  - decision = DENY → respond `{decision: "reject"}` and log audit entry
- Great Sage **never** launches OpenCode in a mode that self-approves
  everything. Auto-approval is acceptable only for classes already
  auto-allowed by Great Sage policy (READ / LOW_WRITE inside trusted roots).
- `--auto`, `ask=never`, or equivalent blanket approval for unrestricted
  `bash`/`edit` is **forbidden** unless the user explicitly approved the
  exact scope; the audit log records every such approval.

## 5. Failure & Recovery

- `health()` gates delegation; unhealthy → mark provider degraded, route to
  LocalModelProvider or report, never hang.
- Session interrupt (`POST /api/session/:id/interrupt`) + Great Sage-side
  timeout/limit caps (wall clock, output bytes, permissions, sessions).
- Disconnects: the manager counts bounded reconnects per run; a lost executor
  ends the delegation `failed` with `event_connection_lost` (no infinite loop).
- Every delegated task carries a Great Sage-owned `completion_criteria` list; a
  task is only COMPLETED when Great Sage-side verification passes.

## 6. Mapping to AIProvider

`OpenCodeProvider` implements `AIProvider` (see INTERFACES.md) so the core
stays provider-agnostic: `generate` maps to create-session → prompt → poll
until idle; `health_check` maps to `GET /api/info`; `cancel` is a documented
no-op (no in-flight handle — the delegation layer owns session interrupts
via `abort_session`). The OpenCode-specific wire stays inside
`greatsage/intelligence/opencode.py`.

## 7. ECC (Everything Claude Code) Wiring

The full ECC library lives outside this repo at `~/.ecc` (271 skills,
67 Claude agents, 92 Claude commands, `.opencode` plugin + 35 OpenCode
commands, MCP catalog). It is integrated into OpenCode in two layers:

- **Global (operator machine, not committed):**
  `~/.opencode/` auto-loads as a global `.opencode` directory —
  `dist/index.js` (built plugin), `commands/` (35, mirror of
  `~/.ecc/.opencode/commands/`), `skills/ecc` (junction →
  `~/.ecc/skills`, all 271 skills), `instructions/`, `prompts/`,
  `tools/`. Keep it in sync after each ECC update with the vendored
  script (rebuilds the plugin and re-copies assets):
  `~/.opencode/update-ecc.ps1`. Verify with `opencode debug config`
  (expect `everything-claude-code:*` agents/commands) and
  `opencode mcp list`.
- **Project (this repo, committed):** `opencode.json` at the repo root
  merges with the global config and adds only Sage bindings —
  `server` (`127.0.0.1:4096`, matching `ai.providers.opencode`),
   `instructions` (Sage docs, relative paths only per
   `docs/DEVELOPMENT_RULES.md` §1 anti-pattern 6), and restrictive `permission`
  (`edit`/`bash`/`task`/`skill`/`question`/`webfetch`/`websearch`/
  `external_directory` = `ask`) so every mutation surfaces for the
  Great Sage SecurityPolicy. Default model is a free-tier Zen model
  (`muse-spark-1.3-contributor-free`, xhigh effort) — the zero-cost rule
  holds; no paid model default, no absolute user paths, no secrets.

MCP policy: the ECC catalog (`~/.ecc/mcp-configs/mcp-servers.json`)
is reference-only. Enable only zero-cost local servers (e.g.
`ccusage-opencode`, `memory`, `sequential-thinking`); paid/network
services (firecrawl, exa, vercel, …) stay disabled unless the operator
explicitly opts in with env-provided keys — never committed.

## 8. Phase 2 Implementation (`greatsage/intelligence/opencode.py`)

Phase 2 lands the **provider connection** only — no delegation workflow, no
permission response, no agent loop:

- Adapter id `opencode`, capabilities `[REMOTE, CODE_EXECUTION, CANCELLATION,
  TEXT_GENERATION]` — STREAMING and TOOL_CALLING are **not** advertised in
  Phase 2, so requesting them raises an explicit `ProviderCapabilityError`
  instead of a silent fallback.
- `init()`/`health()` probe `GET /api/info` (plus an optional best-effort
  `GET /openapi.json` spec fetch for metadata); an unreachable or non-OpenCode
  endpoint ends in `UNAVAILABLE` — the runtime and the CLI keep working.
- Generation: `POST /api/session` → `POST /api/session/:id/prompt` with
  `{text}`, then poll `GET /api/session/:id/message` until `idle`;
  sessions are best-effort cleaned up (`DELETE`). HTTP Basic auth from
  `api_key_env` (+ optional `username_env`, default `opencode`) — read from
  the environment once, never from source.
- What Phase 5B added on top: poll-based event translation, tool/authority
  delegation with permission decisions from the Great Sage
  security layer (`greatsage/delegation/` — manager, secure permission routing,
  bounded polling, `DELEGATION_*` audit events), task handoff and diff review —
  all gated behind the capabilities the provider advertises.
- Deferred pieces (§2–§5 of this doc) remain design contracts where noted;
  the adapter stays deliberately narrower than the eventual client so nothing
  depends on unverified endpoints. `greatsage/integration/` is reserved (empty)
  — the OpenCode wire lives in `greatsage/intelligence/opencode.py`.