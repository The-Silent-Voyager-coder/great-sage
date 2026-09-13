# Architecture

> Status: **Phases 1–10 implemented (5A agent loop, 5B delegation,
> 6 workspace/planning/task + voice, 7 autonomy graph/verify, 8 vision stub,
> 9 hardening, 10 HUD)**. This is the
> contract that all modules must honor. It may be refined by the architect,
> but not silently violated by implementation. Phase 2 added full detail for
> the intelligence section; Phase 3 adds the memory section (§5.2); Phase 4
> adds the tool section (§5.3); Phase 5A adds the agent section (§5.4);
> §5.5 covers the Phase 6 workspace/planning/task substrate (Phase 7
> autonomy, Phase 8 vision, Phase 9 hardening, and Phase 10 HUD ship as
> sibling subsystems — see per-doc contracts).

## 1. Mission

J.A.R.V.I.S. is a modular, local-first, zero-cost personal AI operating system.
It understands objectives, plans, acts through permissioned tools, remembers
useful facts, delegates coding to OpenCode, monitors long-running work, and
verifies results before claiming success.

## 2. Absolute Rules

1. **Zero cost** — no paid APIs, subscriptions, hosting, databases, or SaaS.
   External services are optional, never required.
2. **Local-first** — every capability that can run locally (STT, TTS, wake word,
   memory, embeddings, control, state, logs, config) runs locally.
3. **Provider independence** — the core depends on the `AIProvider`
   abstraction, never on OpenCode or any specific model vendor. Removing
   OpenCode must not break the core.
4. **OpenCode is an agent/tool, not J.A.R.V.I.S.** — J.A.R.V.I.S. owns
   understanding, planning, state, delegation, monitoring, permissions,
   verification, and reporting. OpenCode does implementation work only, through
   its supported HTTP server/API (never screen scraping or UI automation).
5. **No unrestricted computer access** — every action passes the security
   layer. An LLM asking for a shell does not get one.
6. **Infrastructure first, UI later** — no premature animation/HUD work.

## 3. High-Level Diagram

```text
                         USER
                          │
                ┌─────────▼─────────┐
                │  J.A.R.V.I.S.     │
                │  CORE RUNTIME     │   lifecycle, config, events,
                └─────────┬─────────┘   sessions, task coordination
                          │
        ┌─────────────────┼──────────────────┐
        │                 │                  │
        ▼                 ▼                  ▼
    Intelligence       Memory             Planning
   (AIProvider)     (working/long-term/   (tasks, steps,
     abstraction      episodic/semantic)    retries)
        │                 │                  │
        └─────────────────┼──────────────────┘
                          ▼
                  ┌───────────────┐
                  │ Tool System   │  typed, risk-rated, permission-aware
                  └───────┬───────┘
      ┌──────────┬────────┼────────┬──────────┐
      ▼          ▼        ▼        ▼          ▼
   Windows     Files   Terminal  Browser     Git
      │          │        │        │          │
      └──────────┴────────┼────────┴──────────┘
                          ▼
                  ┌───────────────┐
                  │ Security Layer│  allow / ask / deny + risk levels
                  └───────────────┘

        Voice / Text / Future Vision
                    ▼
               JARVIS CORE
                    │
              OpenCode Integration
                    │
             Coding Specialist (DeepSeek V4 Flash)
```

## 4. Module Map

| Module | Responsibility | Phase |
|---|---|---|
| `core/` | Lifecycle, service registry, session coordination, startup/shutdown | 1 |
| `events/` | Event bus, structured serializable events | 1 |
| `configuration/` | Validated config loading (file + env), secrets handling | 1 |
| `observability/` | Structured logs, spans, activity reconstruction | 1 |
| `interface/` | CLI, terminal/simple web/voice entry points | 1 |
| `intelligence/` | `AIProvider` abstraction, local provider, model router, structured output, streaming | 2 |
| `memory/` | Memory models, SQLite storage, provenance, retrieval | 3 |
| `tools/` | Typed tool registry + security pipeline (files, processes, shell, system, network, gui); policy, path security, shell classifier, approvals | 4 + roadmap |
| `agent/` | Bounded AI↔tool loop: validated state machine, limits, loop detection, approval pause, cancellation, events, health | 5A |
| `delegation/` | Controlled OpenCode delegation: manager, limits, service, secure permission routing, bounded SSE | 5B |
| `workspace/` | Workspace discovery, scanner, persistence | 6 |
| `planning/` | Deterministic plan decomposition + task-graph DAG + verification gate (no AI calls) | 6/7 |
| `task/` | Bounded multi-step execution over the tool pipeline | 6 |
| `voice/` | Wake word, STT, TTS local-first pipeline (mock default; Vosk/Piper/fuzzy opt-in, models under `C:/GREATSAGE/models`) | 6 + roadmap A |
| `memory/` | … + local embeddings (Ollama `nomic-embed-text`, `memory_embeddings` table, semantic recall) | 3 + roadmap B |
| `scheduler/` | SQLite-backed recurring jobs (`briefing`/`tool` kinds, bounded `tick`, no daemon) | roadmap C |
| `telegram/` | Remote chat bridge (Bot API long-poll, allowlisted chats, bounded listen) | roadmap C |
| `agents/` | Reserved for composable agents (empty — Phase 7 ships as `planning/graph.py` + `verify.py`) | 7+ (reserved) |
| `security/` | Reserved (empty by design — Phase 9 ships as `tools/redaction.py` + policy tightening) | 9 (patch) |
| `vision/` | Bounded capture, grounding, permissioned tools (stub backend, no OCR) | 8 |
| `interface/` | Local-first HUD status/dashboard (read-only; no `hud/` module) | 10 |
| `integration/` | Reserved (empty — OpenCode wire lives in `intelligence/opencode.py` + `delegation/`) | 5 (via 5B) |
| `storage/` | Local filesystem layout, archive policy (Google Drive = external archive only) | 1 |
| `tests/` | Unit, integration, provider, tool, security, memory, task, OpenCode tests | all |

### Dependency direction

`core` → `events`, `configuration`, `observability` (always available)
`core` → `intelligence`, `memory`, `planning`, `tools`, `security` (subsystems)
`core` → `integration` (OpenCode) — **only via provider abstraction; never direct**

Rules:

- Modules may depend on `events`, `configuration`, `observability` freely.
- No module may depend on `core` internals (inject service handles instead).
- `integration` depends on `intelligence` interfaces, not the reverse.
- `voice`, `vision`, `interface` are leaves: they consume, never get consumed.

## 5. Core Runtime

Responsibilities: initialize services in dependency order, load validated
config, start event bus, register services, run health checks, route events,
maintain active sessions, coordinate tasks, expose internal service
interfaces, shut down cleanly (flush state, revoke permissions, stop loops).

The core **must not** contain provider-specific code:

```text
Bad:      core -> OpenCode
Good:     core -> AIProvider ──└──→ OpenCodeProvider
```

### Phase 1 implementation

Lifecycle states (`greatsage/core/lifecycle.py`):

```text
CREATED → INITIALIZING → RUNNING → STOPPING → STOPPED
                │                        ▲
                └──▶ STOPPING (failure cleanup only)
```

- Every transition is validated; invalid transitions raise `LifecycleError`.
- `Runtime.start()` (async): load config → configure logging → create
  registry/bus/storage/health → register all four as services → health checks
  → `StorageManager.ensure_directories()` → start the Intelligence service →
  dependency-ordered `registry.start_all()` → publish `RuntimeStarted` →
  `RUNNING`. Any failure runs cleanup (stop started services, close bus, flush
  logs) and ends in `STOPPED`, never `RUNNING`.
- `Runtime.stop()` is idempotent and safe before start: stop services in
  reverse dependency order → publish `RuntimeStopping` and `RuntimeStopped` →
  close the bus → flush logs → `STOPPED`.
- Health (`greatsage/core/health.py`): core checks — `core` (lifecycle state),
  `configuration` (loaded + validated), `event_bus` (open and accepting),
  `service_registry` (registered + started), `storage` (data root writable) —
  plus one self-registered check per subsystem (`intelligence` from Phase 2,
  `memory` from Phase 3, `tools` from Phase 4, `agent` from Phase 5A,
  delegation/workspace/planning/task/scheduler/telegram as each landed).
  Overall status = HEALTHY only when every check is HEALTHY,
  DEGRADED when at least one is DEGRADED, otherwise UNHEALTHY.

## 5.1 Intelligence Layer (Phase 2)

The intelligence layer (`greatsage/intelligence/`) implements the Phase 0
`AIProvider` abstraction: provider-neutral models, a provider interface,
registry, deterministic router, adapters, mock provider, read-only benchmark,
and the service facade the runtime owns.

### Package layout

```text
greatsage/intelligence/
├── models.py      provider-neutral AIRequest/AIResponse/StreamChunk/Message/
│                  TokenUsage/ToolCall - the only types the core sees
├── provider.py    ProviderState, ProviderCapabilities, AIProvider ABC,
│                  ProviderHealth; failures become states, never crashed loops
├── registry.py    ProviderRegistry: register (dup-id rejected), get,
│                  enumerate, health, initialize/shutdown, snapshot
├── router.py      deterministic Router + Route record
├── transport.py   stdlib urllib JSON/text helpers (PyYAML stays the only
│                  third-party runtime dependency)
├── ollama.py      OllamaProvider (local) - /api/tags health + /api/chat
├── opencode.py    OpenCodeProvider (remote) - /global/health, /doc, session,
│                  prompt_async - connection only in Phase 2, no delegation
├── mock.py        MockProvider - deterministic, born READY, configurable
│                  failure/latency/capabilities, used heavily in tests
├── benchmark.py   read-only hardware diagnostics (CPU/RAM/GPU/VRAM/Ollama)
│                  - no downloads, no GPU stress, stdlib only
└── service.py     IntelligenceService facade owned by the Runtime
```

### Core models (`models.py`)

`AIRequest`: `messages`, `request_id`, `system_prompt`, `model`,
`temperature`, `max_tokens`, `tools`, `metadata`, `timeout`. Validation:
non-empty messages, tool messages need `tool_call_id`, temperature in
0.0..2.0, positive `max_tokens`/`timeout`. Messages carry `Role`
(system/user/assistant/tool) and either plain text or structured content
parts (`TextPart`/`ToolCallPart`).

`AIResponse`: `request_id`, `provider`, `model`, `content`, `finish_reason`,
`usage`, `tool_calls`, `metadata`. `TokenUsage` fields are `None` when the
provider does not report them — counts are **never fabricated**.

Streaming contract: `async for chunk in provider.stream(request)` yields
`StreamChunk` with kind `text` | `tool_call` | `metadata` | `completion` |
`error`. Streaming is capability-gated: requesting it from a non-streaming
provider is an explicit `ProviderCapabilityError`, never a silent fallback.

### Capabilities and states (`provider.py`)

`Capability`: TEXT_GENERATION, STREAMING, TOOL_CALLING, VISION,
STRUCTURED_OUTPUT, CANCELLATION, LOCAL, REMOTE, CODE_EXECUTION. Every
provider declares its set; the router filters on it.

`ProviderState`: UNINITIALIZED → INITIALIZING → READY | DEGRADED |
UNAVAILABLE | FAILED → STOPPING → STOPPED. `init()` never raises: an
unreachable target ends UNAVAILABLE, a malformed service ends DEGRADED, and
`health()` is a fail-safe probe that also never raises. A stopped provider
reports not-ok.

### Registry (`registry.py`)

Duplicate provider ids are rejected with `ServiceError`. Registry-level
iteration is failure-isolated: `health_all()` probes every provider and
reports per-provider results without raising on any single failure.

### Deterministic router (`router.py`)

Pipeline: request → capability filter → availability filter → policy →
selected provider. Ordered rules:

1. Explicit selection (request metadata `provider`, else the configured
   default) is **never** overridden — an unavailable explicitly-selected
   provider is a hard error, no fallback.
2. Capability filter: required set = TEXT_GENERATION + STREAMING (if
   requested) + TOOL_CALLING (if tools declared) + CODE_EXECUTION (for
   `task_kind: coding`); providers missing any are dropped.
3. Availability filter: only READY providers.
4. Model filter: a request `model` outside a provider's advertised
   `model_ids` drops it.
5. Policy: coding tasks prefer the remote code-execution provider; otherwise
   a local-only provider is preferred; otherwise the first capable candidate.

Every `Route` records `request_id`, `requested_provider`, `selected_provider`,
`reason`, and `alternatives` — routing decisions are always observable and
published as `AIProviderSelected`.

### Service facade (`service.py`)

`IntelligenceService` owns the registry + router, constructs adapters from
`ai.providers` config, exposes `generate` / `stream` / `cancel`, publishes
the `AI*` event stream, and registers the `intelligence` runtime health check
(HEALTHY when at least one provider is healthy). Provider failures surface as
events + states; they never crash the runtime, the bus, or other providers.

### Adapters

- **OllamaProvider** (`local`): probes `/api/tags`, generates via
  `/api/chat`, streams newline-delimited JSON, never downloads models. When
  Ollama is absent the provider reports UNAVAILABLE and the runtime keeps
  working.
- **OpenCodeProvider** (`opencode`): `/global/health` + `/doc` (OpenAPI 3.1)
  for connectivity, `/session/*` for session lifecycle, `prompt_async` for
  generation. Phase 2 scope is provider connection only — no authority
  delegation, no agent loop. API key (when configured) is read from the
  environment variable named by `api_key_env`, never from source.

## 5.2 Memory Layer (Phase 3)

The memory layer (`greatsage/memory/`) implements the Phase 0 memory contract:
four memory categories, typed entries with provenance and confidence, SQLite
persistence with schema-versioned migrations, a repository abstraction,
deterministic retrieval with a documented ranking formula, session-scoped RAM
working memory, memory events, and a runtime health check with failure
isolation. See `docs/MEMORY.md` for the full contract.

### Package layout

```text
greatsage/memory/
├── models.py           MemoryType (working/long_term/episodic/semantic),
│                       Provenance, Memory (typed entry), MemoryFilter,
│                       RankedMemory/MemoryRetrieval, content normalization
├── repository.py       MemoryRepository ABC + RepositoryHealth (persistence
│                       contract the service depends on, never SQL)
├── sqlite_repository.py SqliteMemoryRepository: stdlib sqlite3, schema
│                       versioning + migrations, WAL, FTS5 (+ LIKE fallback),
│                       soft delete, expire sweep, transactional writes
├── working_memory.py   WorkingMemory + WorkingMemoryStore (per-session RAM,
│                       TTL, lazy purge; never auto-promoted to long-term)
├── service.py          MemoryService facade owned by the Runtime
└── __init__.py         package surface (models, service, working memory)
```

### Core models (`models.py`)

`Memory`: `id` (`mem_<hex>` UUID, never sequential), `memory_type`, `content`
(plain string — FTS-indexed — or structured JSON `dict`/`list`), `source`,
`provenance` (canonical kinds in the `Provenance` enum, any string allowed),
`confidence` (0.0–1.0), `created_at`/`updated_at` (timezone-aware UTC),
`expires_at` (nullable), `metadata`, `session_id`, `deleted_at` (auditable
soft delete). `validate()` enforces every invariant; `to_dict(include_content)`
supports redacted CLI/event output.

`MemoryFilter` carries all deterministic filters (memory_type, source,
provenance, created_after/before, expires_before, minimum_confidence,
session_id, include_expired, include_deleted).

### Persistence (`sqlite_repository.py`)

- Schema versioning: `schema_meta` key/value table; migrations are ordered
  stdlib SQL statements; a database with a **newer** schema version is
  refused (never touched), a **corrupted** database degrades to
  `unavailable` with the file **kept as-is** (never deleted to repair).
- WAL mode, `busy_timeout`, a single connection guarded by an `RLock` —
  a small, documented local concurrency strategy (no distributed layer).
- Full-text search: external-content FTS5 table + sync triggers when the
  build supports it, with a safe `LIKE ... ESCAPE '\'` fallback otherwise.
  Search excludes expired/deleted rows by default.
- Writes are transactional: create/update/delete/expire never leave partial
  rows; `update()` atomically replaces the row.

### Repository abstraction (`repository.py`)

`MemoryRepository` ABC: `create/get/update/delete/list/search/expire/count/
stats/initialize/close/health`. `list()`/`search()` return the full matching
set ordered `created_at DESC, id ASC` — ranking and pagination belong to the
service. `RepositoryHealth` reports `accessible`, `schema_valid`,
`migrations_current`, `writable`, `fts_enabled`, `schema_version`,
`database_path`, `detail`.

### Service facade (`service.py`)

`MemoryService` owns the repository + working-memory store, constructs the
SQLite repository from `memory.database_path`, and exposes:

- `remember(content, ...)` — deliberate save (never automatic), confidence
  defaults to `memory.default_confidence`, per-type retention from
  `memory.retention_days` for WORKING/EPISODIC when no `expires_at` given
- `record_episode(...)` / `add_semantic(...)` — typed convenience writes
- `retrieve(query=None, filters, limit, offset)` — deterministic ranking
  (`0.5·relevance + 0.3·confidence + 0.2·recency`, recency =
  `1/(1+age_days)`), each result carries a `match_reason`; pagination via
  limit/offset; `total` counts the full match set
- `get/update/forget` — update preserves provenance/source/type/session and
  always bumps `updated_at`; forget is an auditable soft delete
- `expire()` — sweeps expired memories into deleted state, emitting
  `MemoryExpired`
- `working(session_id)` / `drop_working_session` — RAM per-session store,
  strict session isolation
- `health()` / `register_health_check(...)` — registers the `memory` runtime
  health check; HEALTHY when the DB is accessible/schema-valid/current/
  writable, UNHEALTHY otherwise. A disabled-by-config subsystem reports
  HEALTHY (intentional no-op); a failed init reports `unavailable` with a
  detail string — the runtime and CLI keep running.

Memory events (`MemoryCreated/Updated/Deleted/Expired/Retrieved`) carry only
`memory_id`, `memory_type`, `source`, `provenance`, `session_id` — never
content (privacy rule, `docs/SECURITY_MODEL.md` §8).

## 5.3 Tool Layer (Phase 4)

The tool layer (`greatsage/tools/`) implements the Phase 0 permission-aware tool
contract as an executable security pipeline. Full contract: `docs/TOOLS.md`.

### Package layout

```text
greatsage/tools/
├── models.py            BaseTool, ToolRequest, ToolContext, ToolResult,
│                        ToolRisk (safe..critical), ToolCategory, ToolDecision,
│                        ApprovalOutcome, validate_arguments (schema subset)
├── registry.py          ToolRegistry: register/get/list_ids/describe/health;
│                        duplicates and invalid schemas rejected
├── policy.py            SecurityPolicy + mode matrix (normal/lockdown/
│                        development) + hooks (path, shell, sensitive)
├── pathsecurity.py      canonicalize, is_within, protected-file detection
├── shell_classifier.py  safe/restricted/dangerous/forbidden command tables
├── environment.py       scrub_environment / merge_environment (no secrets leak)
├── approval.py          ApprovalProvider ABC (approved/denied/timeout/cancelled)
├── filesystem_tools.py  list/stat/read/mkdir/write (no delete tool)
├── process_tools.py     list/info (no terminate tool)
├── system_tools.py      system.info (OS/CPU/RAM/storage/GPU, redacted)
├── shell_tools.py       shell.execute (explicit argv, never shell=True)
├── service.py           ToolService facade: the single security pipeline
├── defaults.py          DEFAULT_TOOL_CLASSES + register_default_tools()
└── __init__.py          package surface
```

### Pipeline

`AI → ToolRequest → ToolRegistry → SecurityPolicy → ALLOW|ASK|DENY → Approval
→ Execution → ToolResult → audit events`. There is no bypass: the CLI
`tools execute` command drives the exact same `ToolService.execute`.

### Key properties

- Decisions are `ALLOW`/`ASK`/`DENY`, never booleans; `critical` risk is
  denied in every mode; `lockdown` allows only `safe`; an `ASK` with no
  approval provider is denied (fail-closed).
- Policy hooks only tighten: path checks (allowed/denied roots, protected
  files), shell classifier, sensitive-argument detection.
- Execution bounds: no `shell=True`, per-run timeout, bounded output with
  truncation markers, scrubbed environment, explicit working directory.
- Audit events (`TOOL_*`) carry `request_id`/`tool_id`/`risk_level`/
  `session_id`/`task_id`, never sensitive argument values.
- No `filesystem.delete` and no `process.terminate` exist in Phase 4;
  `network`/`browser`/`gui` categories are reserved.
- Failure isolation: a broken tool configuration leaves the service
  `unavailable` with a detail string; the runtime and CLI keep working.

## 5.4 Agent Layer (Phase 5A)

The agent layer (`greatsage/agent/`) implements the bounded, auditable
AI→tool→result→AI loop over the Phase 4 ToolService. The orchestrator holds
the loop logic; the service owns provider gating, limits, approval swap,
memory retrieval, cancellation, and health. Authority hierarchy:
**AI ≠ authority; Tool Security = authority; Agent Orchestrator = control
flow** (see `docs/AGENTS.md`).

### Package layout

```text
greatsage/agent/
├── models.py           AgentTask, AgentStep, ToolCall (id/tool_id/structured
│                       arguments/sequence — never an opaque shell string),
│                       ToolCallResult, AgentResult, AgentRunStatus,
│                       AgentLimits (hard-ceilinged), AgentState (validated
│                       state machine), AGENT_* event constants
├── limits.py           default limits + ceilings; the smaller applicable
│                       limit wins (12/8/300/3/2MiB/3 defaults,
│                       25/50/1800/10/16MiB/25 ceilings)
├── loopdetect.py       LoopDetector: rolling tool-call signature window
│                       with exact-match threshold burst detection
├── cancellation.py     CancellationToken: cooperative cancel requested/cancelled
├── approval.py         AgentApprovalProvider: thread-safe async decision
│                       gate (approve_all/disapprove_all/DENIED, timeout,
│                       cancellation); decisions are snapshotted under the
│                       lock and consumed outside it (no re-entrant deadlock)
├── orchestrator.py     AgentOrchestrator: deterministic bounded loop +
│                       AGENT_* event stream
├── service.py          AgentService facade owned by the Runtime
└── __init__.py         package surface
```

### State machine

Every transition is validated; invalid transitions raise `AgentStateError`.

```text
PENDING → RUNNING → EXECUTING_TOOL → (WAITING_FOR_APPROVAL ⇄ EXECUTING_TOOL)
   │          │                │              │
   │          ▼                ▼              ▼
   │      COMPLETED        FAILED          (terminal only via RUNNING)
   └──? CANCELLED / TIMED_OUT / LIMIT_REACHED
```

Terminal states: `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`,
`LIMIT_REACHED`. `WAITING_FOR_APPROVAL` can only transition back to
`RUNNING`/`EXECUTING_TOOL` (after approval) or through `RUNNING` to a
terminal state (on timeout/denial-resolve).

### Loop (AgentOrchestrator)

1. `AGENT_STARTED`; loop while `RUNNING` and limits allow.
2. `AGENT_STEP_STARTED` → provider generate (provider-neutral
   `AIRequest`; `max_steps` default applied).
3. `AGENT_TOOL_CALL_REQUESTED` (one tool call per step, strictly
   sequential — no parallel calls) → loop detection check →
   `ToolService.execute` (the **only** path to a tool; the security
   pipeline incl. approvals always runs — no internal bypass).
4. Per-call limit checks (`max_single_tool_calls`);
   `AGENT_TOOL_CALL_COMPLETED` or `AGENT_TOOL_CALL_FAILED`; the
   result — including an approval denial, fed back as a failed
   `ToolCallResult` — becomes a tool message for the next step.
5. `AGENT_STEP_COMPLETED`; memory retrieval is injected as bounded
   context (`_MEMORY_REFERENCE_LIMIT = 5`, silent degradation) — never
   automatic long-term saves.
6. `AGENT_COMPLETED` when the provider stops requesting tools; a
   provider failure ends `FAILED`, wall-clock timeout ends `TIMED_OUT`,
   and limit exhaustion ends `LIMIT_REACHED` with the exact reason
   (`max_steps`/`max_tool_calls`/`max_wall_time`/`max_single_tool_calls`/
   `max_total_tool_output`/`loop_detection`).

Cancellation is cooperative: `CancellationToken.cancel()` flips the token;
the orchestrator checks it between steps and before every tool call; a
cancelled per-call approval gate resolves the pending tool call as
cancelled and ends the run `CANCELLED`.

### Limit hierarchy

Config `agent:` block + run-time overrides vs. hard ceilings
(`greatsage/agent/limits.py`): the smaller applicable limit wins. This is a
runtime invariant, not a documentation convention.

### Service facade (service.py)

`AgentService` gates on `agent.enabled`, requires the provider registry to
be wired, requires a provider that is owned (explicit or configured default)
with `TOOL_CALLING` capability and `READY` state — otherwise typed errors
(`AgentUnavailableError`, `ProviderUnavailableError`, `ProviderCapabilityError`),
never silent fallback. It swaps in an `AgentApprovalProvider` before the run
(restoring the previous provider after), retrieves bounded memory context,
exposes `run(prompt, ...)`/`cancel()`/`health()`, registers the `agent`
runtime health check (`available`/`status`/`enabled`/`detail`/`current`),
and publishes `AGENT_*` events with exactly one terminal state per failure.

## 5.5 Workspace / Planning / Task Layer (Phase 6)

Phase 6 adds the execution substrate that Phase 7 autonomy will build on:
workspace discovery (`greatsage/workspace/`), deterministic plan decomposition
(`greatsage/planning/` — template/rule based, **no AI provider calls**), and
bounded multi-step task execution (`greatsage/task/` — steps run exclusively
through the Phase 4 tool pipeline, so allow/ask/deny, scope checks, and
audit entries apply unchanged).

```text
greatsage/workspace/     models, limits, scanner (depth/entry caps, timeout),
                      SQLite repository, service facade (scan/info/health)
greatsage/planning/      models (Plan/Step, 1..n sequence validation),
                      limits (max_plan_steps + ceiling), deterministic
                      Planner (goal-clause → known-safe tool mapping),
                      SQLite repository, service facade
                      (create_plan/get/list/health)
greatsage/task/          models (TaskReport, TaskState machine), limits,
                      cancellation, executor (per-step + total timeouts,
                      unknown-tool and protected-path denial),
                      SQLite repository, service facade
                      (run/resume/list/get/cancel/health)
```

Runtime wiring (`greatsage/core/runtime.py`): the Runtime owns all three
services, starts them from `workspace:`/`planning:`/`task:` config blocks
(enabled-gated, ceiling-validated), registers `workspace`/`planning`/`task`
health checks, and exposes them as `runtime.workspace/planning/task` for
the CLI. Shutdown order is task → planning → workspace. CLI surface:
`greatsage workspace|planning|task …` (see `docs/INTERFACES.md` §12).

Boundaries: the planner never emits unknown tool IDs (fixed allow-list
mirroring the Phase 4 registry) and never executes anything — execution
belongs to the task executor. Phase 7 (autonomy) adds the planner +
task-graph + verification loop on top; it must not bypass these facades.

Phases 6 (voice), 8 (vision), 10 (HUD) ship as sibling subsystems with
their own CLIs (`greatsage voice|vision|hud …`); vision currently has no
`vision.*` config section (config-independent service — see integration
notes). Phase 9 (security hardening) is done: secret redaction, path/command
policy tightening, audit safety.

## 6. Event System

All module-to-module coupling that is not a direct service call goes through
the event bus. Events are structured and JSON-serializable:

```text
UserMessageReceived        ToolRequested          TaskCreated
VoiceWakeDetected          ToolStarted            TaskStarted
SpeechTranscribed          ToolCompleted          TaskPaused
AIResponseStarted          ToolFailed             TaskCompleted
AIResponseCompleted        PermissionRequested    TaskFailed
OpenCodeConnected          PermissionGranted      MemoryCreated
OpenCodeDisconnected       PermissionDenied       MemoryRetrieved
OpenCodeEventReceived      RuntimeStarted         RuntimeStopping
RuntimeStopped             ...
```

Phase 2 added the intelligence event family (all published by the
IntelligenceService, payloads are JSON-safe and never include prompts or
secrets):

```text
AIRequestStarted         AIProviderSelected      AIStreamStarted
AIRequestCompleted       AIProviderUnavailable   AIStreamCompleted
AIRequestFailed                                  AIStreamFailed
```

Phase 3 added the memory event family (published by the MemoryService;
payloads carry ids/types/sources only, never content):

```text
MemoryCreated   MemoryUpdated   MemoryDeleted   MemoryExpired   MemoryRetrieved
```

Phase 4 added the tool event family (published by the ToolService; payloads
carry request/tool/risk/session/task ids and reasons, never sensitive
argument values):

```text
ToolRequested   ToolAllowed   ToolApprovalRequested
ToolApproved    ToolRejected  ToolStarted  ToolCompleted  ToolFailed
ToolDenied
```

Phase 5A added the agent event family (published by the AgentService/
AgentOrchestrator; payloads carry ids/reasons only — prompts are never
published):

```text
AGENT_STARTED              AGENT_TOOL_CALL_FAILED
AGENT_STEP_STARTED         AGENT_STEP_COMPLETED
AGENT_TOOL_CALL_REQUESTED  AGENT_COMPLETED
AGENT_TOOL_CALL_COMPLETED
```

Phase 4 `TOOL_*` events keep flowing for every tool execution inside an
agent run; the exact event order for one tool call is
`AGENT_STARTED → AGENT_STEP_STARTED → AGENT_TOOL_CALL_REQUESTED →
AGENT_TOOL_CALL_COMPLETED → AGENT_STEP_COMPLETED → AGENT_STEP_STARTED →
AGENT_STEP_COMPLETED (final) → AGENT_COMPLETED`.

Event envelope: `{id, type, timestamp, session_id?, task_id?, source, payload}`.
The bus must support publish/subscribe, per-type routing, and ordered
delivery per source. Persisted event logs are part of observability.

### Phase 1 implementation

`greatsage/events/models.py` defines the immutable `Event` envelope (above) and
the catalog of event-type constants; `RuntimeStarted`/`RuntimeStopping`/
`RuntimeStopped` were added in Phase 1 to report lifecycle transitions on the
bus. `greatsage/events/bus.py` implements `EventBus`:

- `subscribe(type | None, handler)` / `unsubscribe` / `clear`; handlers may be
  sync callables or coroutines.
- `publish(event)` is ordered (subscribers run in subscription order) and
  isolated: one failing subscriber is logged, never aborts the bus.
- `publish_nowait` schedules without awaiting; `close()` is idempotent and
  rejects further publishes with `EventError`.

## 7. Storage Layout (Windows)

```text
C:\GREATSAGE\
├── app\          → installed code (repo)
├── data\         → SQLite DBs, task state, memory
├── models\       → local model files (STT/TTS/embeddings)
├── workspaces\   → active dev workspaces (git repos)
├── cache\        → transient caches
├── logs\         → structured logs
├── backups\      → archives destined for Google Drive
└── runtime\      → PID files, sockets, ephemeral state
```

Host platform names (`C:\GREATSAGE`) are **defaults in config**, never hard-coded.

### Google Drive

Google Drive is an **external archive/backup** destination only. Active
development never happens inside a synced Drive workspace. J.A.R.V.I.S. must
not assume Drive is mounted or synced.

## 8. Observability

Structured logs (JSON) with fields: `timestamp, session_id, task_id, component,
event, action, result, duration, error`. Goal: the user can ask "what were you
doing for the last two hours?" and J.A.R.V.I.S. can reconstruct its activity
from logs + task history.

### Phase 1 implementation

`greatsage/observability/logging.py`:

- `setup_logging(cfg, logs_dir, console=True)` configures the `jarvis`
  logger: one JSON record per line to stdout and a rotating
  `<logs_dir>/jarvis.log` (10 MB, `backupCount = retention_days`).
- Records carry `timestamp, level, logger, message, component, event_id,
  session_id, task_id` plus any extra context; correlation IDs are bound via
  `correlation()` context manager (context variables), so logs inside a
  task/session inherit them automatically.
- Redaction: values under secret-shaped keys (`apiKey`, `password`, `token`,
  `secret`, `authorization`, …) and URL userinfo are masked (`***`) in JSON
  output as defense in depth — logging code must still never log credentials.

## 9. Verification (Definition of "done")

J.A.R.V.I.S. distinguishes *"I think it worked"* from *"the test/build/check
actually succeeded."* Coding tasks follow:

```text
implementation → build → tests → static checks → review → final report
```

If verification fails at any stage, the task remains incomplete. Completion
criteria are explicit fields of every task (see `docs/INTERFACES.md`).

## 10. Autonomy Guardrails

Autonomy is *goal → plan → act → observe → verify → adjust*, never "LLM gets
unrestricted computer access." Every autonomous loop requires: max iteration
count, timeout, resource limit, permission enforcement, failure handling,
cancellation, and audit logging.