# Great Sage

**Wise One — state your query.**

A modular, local-first, zero-cost personal AI operating system for Windows 11.
Formerly Great Sage (Just A Rather Very Intelligent System) — kept here
as lore; the architecture continues unchanged under its new name.

Great Sage is **not** a chatbot. It is an infrastructure-first platform that
will eventually converse naturally (voice + text), remember, plan, run tools,
control the computer safely, delegate coding work to OpenCode, monitor
long-running objectives, and recover from failures.

| | |
|---|---|
| Platform | Windows 11 |
| Cost | ₹0 / $0 — no paid APIs, no paid hosting, no paid cloud |
| Language | Python 3.11+ |
| Status | **Phase 5B — Controlled OpenCode delegation** (Great Sage is authority, OpenCode is delegated executor) |

---

## Current Status (Phase 4)

Phases 1–3 delivered the core runtime, the intelligence layer, and the memory
foundation; Phase 4 adds the permissioned tool system on top of all three:

- **One security pipeline, no bypass**: AI → `ToolRequest` → registry →
  security policy (ALLOW/ASK/DENY) → approval → execution → audit events.
  The CLI `tools execute` command drives the identical code path.
- **Typed tool registry**: 15 built-in tools (`filesystem.list|stat|read|
  mkdir|write`, `process.list|info`, `system.info`, `shell.execute`,
  `network.fetch`, `homeassistant.states|call`, `gui.screenshot|click|type`) with
  declared risk levels and JSON-schema validation; duplicate ids and invalid
  schemas are rejected at registration
- **Risk levels + modes**: `safe`/`low`/`medium`/`high`/`critical` with a
  per-mode decision matrix (`normal`/`lockdown`/`development`); `critical`
  tools are denied in every mode; `lockdown` allows only `safe`; policy `ASK`
  without an approval provider is denied (fail-closed)
- **Policy hooks that only tighten**: path security (allowed/denied roots,
  protected files like `memory.db`/`.env`, secret-stemmed names), shell
  command classifier (`safe`/`restricted`/`dangerous`/`forbidden`, case- and
  `.exe`-insensitive), and sensitive-argument detection (credentials denied)
- **Execution bounds**: no `shell=True` anywhere; every run has a timeout,
  bounded output (truncation marker), a scrubbed environment (Great Sage secrets
  never reach tools), and an explicit working directory
- **Intentional absences**: no `filesystem.delete` and no `process.terminate`
  tool; `network`/`browser`/`gui` categories reserved for later phases
- **Audit events**: every attempt (including denials) published with
  `request_id`/`tool_id`/`risk_level`/`session_id`/`task_id`; sensitive
  argument values are never included
- **Failure isolation**: a broken tool configuration degrades the subsystem
  to `unavailable` while the rest of the runtime keeps working
- **CLI**: `greatsage tools list|info|health|execute [--json] [--approve]`
- **No new dependencies**: stdlib only; PyYAML remains the sole runtime
  dependency

## Current Status (Phase 5A)

Phase 5A adds the bounded, auditable AI↔tool loop on top of all four layers:

- **Bounded AI↔tool loop**: the agent proposes one structured tool call per
  step → the tool security pipeline executes it → the result feeds the next
  step; provider-neutral models throughout (never OpenCode-specific)
- **No bypass**: the loop drives `ToolService.execute` only — the same
  policy/approval/audit pipeline as `greatsage tools execute`
  (**AI ≠ authority; Tool Security = authority; Agent Orchestrator =
  control flow**)
- **Validated state machine**: `pending → running ⇄ executing_tool ⇄
  waiting_for_approval`; exactly one terminal state per run — `completed` /
  `failed` / `cancelled` / `timed_out` / `limit_reached` (see
  `docs/AGENTS.md`)
- **Hard limits with ceilings**: step count, tool-call count, wall clock,
  per-tool repeat count, cumulative output bytes, and loop detection
  (exact-match signatures); the smaller applicable limit always wins
- **Approval is a real gate**: `ASK`-level calls pause the run in
  `WAITING_FOR_APPROVAL` until a human decides; denials are fed back to the
  LLM as failed tool results — never auto-approved
- **Cooperative cancellation** at every checkpoint + wall-clock timeout +
  `AGENT_*` audit events (no prompts, no secrets; Phase 4 `TOOL_*` events
  keep flowing)
- **CLI**: `greatsage agent health|run --prompt … [--json]`; exit 0/1/2
- **Tests**: full agent unit suite (incl. a threaded approval-deadlock
  regression), offline CLI integration suite, scripted mock-provider
  tool-call tests; ruff + mypy clean

**Shipped since**: autonomy task-graph + verification gate (Phase 7),
security hardening — secret redaction + policy tightening (Phase 9) —
voice pipeline with stub backends, no model downloads (Phase 6), vision
with stub backend, no OCR (Phase 8), read-only HUD status/dashboard
(Phase 10), and OpenCode delegation (Phase 5B). Semantic-memory ingestion
remains future work.

## Current Status (Phase 5B)

Phase 5B adds controlled OpenCode delegation under full Great Sage authority:

- **Great Sage is authority, OpenCode is executor**: `USER → Great Sage → AgentOrchestrator → DelegationManager → Great Sage SecurityPolicy → OpenCode Provider → OpenCode Server → SSE → Great Sage events`. OpenCode never bypasses the security layer.
- **Provider-neutral delegation**: `DelegationManager` talks only to a `DelegationProvider` protocol (`Capability.DELEGATION`); OpenCode-specific wire formats stay in `greatsage/intelligence/opencode.py`.
- **Secure permission routing**: every executor permission (`read`/`edit`/`write`/`bash`/`webfetch`/`websearch`/`task`/`skill`…) is mapped to a Great Sage `ToolCategory`/`ToolRisk`; unknown actions default to `HIGH`/`SYSTEM` and are `ASK`-gated; `ALLOW` auto-approves only `SAFE`/`LOW` inside allowed roots, `DENY` blocks `CRITICAL`/forbidden commands and protected paths.
- **Hard-bounded execution**: `max_wall_time_seconds` (1800s default, 7200s ceiling), `max_output_bytes` (4 MiB/32 MiB), `max_permission_requests` (50/200), `max_session_count` (3/10), `max_delegation_depth` (1 — no recursion), `SSE_RECONNECT_LIMIT` (3) — validated at config load and re-validated per run.
- **Full lifecycle**: `DelegationState` (`created → starting → running ⇄ waiting_for_permission → completing → completed/failed/cancelled/timed_out`), bounded SSE with malformed-event tolerance, timeout/cancellation/diff retrieval, session cleanup, and `DELEGATION_*` + `TOOL_*` audit events (no prompts, no secrets).
- **CLI**: `greatsage delegation health|list|get <task_id>|cancel <task_id> [--json]` (same auth path as
`greatsage agent`/`tools`).
- **Tests**: 1050 passing (13 Sept 2026; incl. 55 delegation, 32 OpenCode),
  `ruff`/`mypy` clean.

## Development Phases

| Phase | Goal | Status |
|---|---|---|
| 0 | Architecture & foundation | **Done** |
| 1 | Core runtime (lifecycle, config, events, CLI) | **Done** |
| 2 | Intelligence (AIProvider abstraction, model router) | **Done** |
| 3 | Memory (SQLite, provenance) | **Done** |
| 4 | Tools (files, terminal, processes, system) | **Done** |
| 5A | Agent loop (bounded AI↔tool, limits, approval, cancellation) | **Done** |
| 5B | Controlled OpenCode delegation (DelegationManager, secure permission routing, bounded SSE) | **Done** |
| 5 | OpenCode integration | **Done** (delivered via 5B delegation layer) |
| 6 | Workspace/planning/task foundation + voice pipeline (wake word, STT, TTS) | **Done** |
| 7 | Autonomy (planner, task graph, verification) | **Done** |
| 8 | Vision (bounded capture, OCR-free grounding, permissioned tools) | **Done** |
| 9 | Security hardening | **Done** |
| 10 | HUD interface (local-first status/dashboard) | **Done** |

## Repository Layout

```text
greatsage/
├── docs/            → architecture & engineering documents (read first)
├── config/          → example configuration
├── greatsage/       → Python package; one module per subsystem
│   ├── core/            → lifecycle, registry, health, runtime
│   ├── configuration/   → typed config loader + validator
│   ├── events/          → event bus + catalog
│   ├── observability/   → structured logging
│   ├── intelligence/    → providers, models, router, benchmark (Phase 2)
│   ├── memory/          → memory models, SQLite persistence, retrieval (Phase 3)
│   ├── tools/           → tool registry, security policy, built-in tools (Phase 4)
│   ├── agent/           → bounded tool loop, limits, approval, events (Phase 5A)
│   ├── delegation/      → controlled OpenCode delegation, limits, manager, service (Phase 5B)
│   ├── workspace/       → workspace discovery, scanner, persistence (Phase 6)
│   ├── planning/        → deterministic plan decomposition, no AI calls (Phase 6)
│   ├── task/            → bounded multi-step execution over the tool pipeline (Phase 6)
│   ├── voice/           → wake word, STT, TTS local-first pipeline (Phase 6)
│   ├── vision/          → bounded capture, grounding, permissioned tools (Phase 8)
│   ├── interface/       → local-first HUD status/dashboard (Phase 10)
│   └── ...              → autonomy (Phase 7, later)
├── tests/           → test suite (per-module subdirectories)
├── .env.example     → secret template (real secrets never committed)
└── pyproject.toml   → project metadata; PyYAML is the only runtime dependency
```

## Quick Start

> State your query. Great Sage acknowledges.

```powershell
# create the virtual environment and install (editable, with dev tools)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# validate the configuration (defaults or your own file)
.\.venv\Scripts\greatsage.exe config validate
.\.venv\Scripts\greatsage.exe config validate --config config\sage.example.yaml

# boot the runtime and report component health
.\.venv\Scripts\greatsage.exe health

# inspect the AI provider layer (works with Ollama running or absent)
.\.venv\Scripts\greatsage.exe ai health
.\.venv\Scripts\greatsage.exe ai providers
.\.venv\Scripts\greatsage.exe ai benchmark

# inspect the memory subsystem (persistent SQLite; database created on first use)
.\.venv\Scripts\greatsage.exe memory health
.\.venv\Scripts\greatsage.exe memory stats
.\.venv\Scripts\greatsage.exe memory search "api key" --content
.\.venv\Scripts\greatsage.exe memory list --type long_term
.\.venv\Scripts\greatsage.exe memory digest --days 7
.\.venv\Scripts\greatsage.exe memory get mem_<id> --content
.\.venv\Scripts\greatsage.exe memory delete mem_<id>   # auditable soft delete

# daily brief: health + recent episodes + open tasks/plans + delegation
.\.venv\Scripts\greatsage.exe briefing
.\.venv\Scripts\greatsage.exe briefing --days 7 --content

# inspect and drive the secure tool system (files/processes/shell/system)
.\.venv\Scripts\greatsage.exe tools list
.\.venv\Scripts\greatsage.exe tools info filesystem.write
.\.venv\Scripts\greatsage.exe tools health
.\.venv\Scripts\greatsage.exe tools execute system.info
.\.venv\Scripts\greatsage.exe tools execute shell.execute --approve \
    'command=["python", "--version"]'
# medium/high-risk tools need --approve; dangerous commands and paths
# outside allowed roots are denied by policy either way

# inspect and drive the bounded agent loop (needs a tool-calling provider)
.\.venv\Scripts\greatsage.exe agent health
.\.venv\Scripts\greatsage.exe agent run --prompt "summarize this repo"
.\.venv\Scripts\greatsage.exe agent run --prompt "check the system" --json
# runs are always bounded: step/tool-call/wall-clock limits with ceilings

# inspect and drive controlled delegation (needs OpenCode server at 127.0.0.1:4096)
.\.venv\Scripts\greatsage.exe delegation health
.\.venv\Scripts\greatsage.exe delegation list
.\.venv\Scripts\greatsage.exe delegation get <task_id>
.\.venv\Scripts\greatsage.exe delegation cancel <task_id>
# delegation is provider-neutral, bounded, and always via Great Sage SecurityPolicy

# recurring local jobs (no daemon — tick directly or from Task Scheduler)
.\.venv\Scripts\greatsage.exe schedule add --name morning --kind briefing --every 86400
.\.venv\Scripts\greatsage.exe schedule tick

# remote chat from your phone (needs TELEGRAM_BOT_TOKEN + allowlisted chat)
.\.venv\Scripts\greatsage.exe telegram health
.\.venv\Scripts\greatsage.exe telegram listen --once

# run the test suite, linter, and type checker
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy greatsage
```

Exit codes: `0` success, `1` general failure (e.g. a provider unhealthy, a
memory not found, or an agent run that did not complete), `2` invalid
configuration/input.

The `ai` commands probe configured providers (`ai.providers.*`); Ollama
absent or not running is fine — the provider reports `unavailable` and the
CLI still exits cleanly (exit `1` from `ai health`). No models are ever
downloaded by Great Sage The `memory` commands need no AI services at all:
they read/write the local SQLite database configured under `memory.*`.
Memory content is shown only with `--content`; every command supports
`--json`.

## Reading Order

1. `docs/ARCHITECTURE.md` — how Great Sage is built
2. `docs/DEVELOPMENT_RULES.md` — hard engineering rules for contributors/agents
3. `docs/INTERFACES.md` — core interfaces (AIProvider, memory, events, tasks, agent)
4. `docs/CONFIGURATION.md` — how configuration works
5. `docs/MEMORY.md` — memory schema, lifecycle, retrieval, privacy
6. `docs/SECURITY_MODEL.md` — permissions and risk levels
7. `docs/TOOLS.md` — the Phase 4 tool system (pipeline, policy, CLI)
8. `docs/AGENTS.md` — the Phase 5A bounded agent loop (rules, state machine, limits)
9. `docs/OPENCODE_INTEGRATION.md` — how OpenCode is integrated
10. `docs/TESTING.md` — testing strategy
11. `docs/DEPENDENCY_POLICY.md` — dependency rules

## Non-Goals (now)

- Decorative interfaces and animations (HUD ships read-only status/dashboard only — Phase 10)
- Cloud/paid voice, vision, or model services (all pipelines are local-first, zero-cost)
- Unattended multi-agent orchestration (Phase 7; the Phase 5A loop is
  single-run and user-initiated)
- Unbounded autonomy (delegation is bounded: depth 1, 30 min wall-clock, 4 MiB output, 50 permissions, 3 sessions)
- Any paid service integration