# Core Interfaces

> Status: **Phases 1–10 implemented (5A agent loop, 5B delegation,
> 6 workspace/planning/task + voice, 7 autonomy, 8 vision stub,
> 9 hardening, 10 HUD)**. Section 1 is implemented as
> the provider abstraction and health contract (`greatsage.intelligence`);
> sections 6 and 7 remain implemented (`greatsage.core.registry`,
> `greatsage.configuration`); §9 documents the implemented memory interfaces
> (`greatsage.memory`); §10 documents the implemented tool system
> (`greatsage.tools`); §11 documents the implemented agent system
> (`greatsage.agent`); the remaining sections stay design contracts for later
> phases. Notation: Python typing + dataclass sketches; exact package layout
> may shift, semantics must not.

## 1. AIProvider Abstraction

```python
class ProviderCapabilities:
    streaming: bool
    tool_calls: bool
    structured_output: bool
    model_ids: list[str]
    context_window: int | None
    max_output_tokens: int | None

class GenerationRequest:
    messages: list[Message]
    model: str | None           # provider-selected if None
    temperature: float | None
    max_tokens: int | None
    tools: list[ToolSpec] | None
    response_schema: dict | None    # structured output
    timeout_seconds: float | None

class GenerationResult:
    text: str
    tool_calls: list[ToolCall] | None
    model: str
    usage: TokenUsage | None
    finish_reason: str

class AIProvider(Protocol):
    def get_capabilities(self) -> ProviderCapabilities: ...
    def generate(self, request: GenerationRequest) -> GenerationResult: ...
    def stream(self, request: GenerationRequest) -> Iterator[StreamChunk]: ...
    def tool_call(self, request: ToolCallRequest) -> ToolCallResult: ...
    def health_check(self) -> ProviderHealth: ...
    def cancel(self, request_id: str) -> None: ...
```

Requirements:

- synchronous + streaming responses
- tool calls
- structured output (`response_schema`)
- model identification and token/context metadata where available
- cancellation (`cancel(request_id)`)
- timeout handling (per-request override)

### Initial providers

| Provider | Uses | Notes |
|---|---|---|
| `LocalModelProvider` | Ollama (or equivalent local engine) | nothing hard-coded until hardware benchmarking |
| `OpenCodeProvider` | OpenCode HTTP server (default `http://127.0.0.1:4096`) | coding/deliberate-agent tasks only; wrapped as an AIProvider so the core never depends on it directly |

Future providers: registration via config (type + factory), no core changes.

### Model router

`intelligence/` exposes a router that selects a provider per request based on
configurable criteria: task class (coding vs reasoning vs quick reply),
provider health, availability, resource budget. Routes are config-driven
(`config/sage.example.yaml → ai`), never hard-coded in core.

### Phase 2 implementation (`greatsage/intelligence/`)

The Phase 0 sketch became an executable provider layer. The core-facing
surface differs in names but preserves every semantic:

```python
# greatsage/intelligence/models.py
class AIRequest:            # was GenerationRequest
    request_id: str
    messages: list[Message] # role: system|user|assistant|tool; content is
                            # str or list[TextPart | ToolCallPart]
    system_prompt: str | None
    model: str | None
    temperature: float | None
    max_tokens: int | None
    tools: list[ToolDefinition] | None
    metadata: dict          # e.g. {"task_kind": "coding", "provider": ...}
    timeout: float | None

class AIResponse:           # was GenerationResult
    request_id: str
    provider: str
    model: str
    content: str
    finish_reason: FinishReason
    usage: TokenUsage | None    # None when provider reports nothing
    tool_calls: list[ToolCall] | None
    metadata: dict

class StreamChunk:          # provider-neutral streaming unit
    kind: "text" | "tool_call" | "metadata" | "completion" | "error"
```

- `AIProvider` (abstract base in `greatsage/intelligence/provider.py`) exposes
  `provider_id()`, `capabilities()`, `health()`, `init()`/`stop()`,
  `generate(request)` (async), `stream(request)` (async generator), and
  `cancel(request_id)`. `stream()` is gated on the STREAMING capability and
  raises `ProviderCapabilityError` otherwise — unsupported features are
  explicit errors, never silent no-ops.
- Capabilities are an enum (`Capability`): TEXT_GENERATION, STREAMING,
  TOOL_CALLING, VISION, STRUCTURED_OUTPUT, CANCELLATION, LOCAL, REMOTE,
  CODE_EXECUTION. Providers advertise only what they actually support.
- Requests validate on construction (empty messages, tool messages without
  `tool_call_id`, temperature/max_tokens/timeout ranges).
- Mock provider (`greatsage/intelligence/mock.py`): deterministic responses,
  streaming, configurable latency/failure, born READY — the test workhorse
  and fail-safe default.
- Adapters (`ollama.py`, `opencode.py`) talk HTTP via stdlib `urllib`; the
  `transport.py` helper distinguishes server errors from network failures so
  unreachable services degrade to UNAVAILABLE instead of raising.

## 2. Event Envelope

```python
@dataclass(frozen=True)
class Event:
    id: str                    # uuid
    type: str                  # e.g. "TaskCompleted"
    timestamp: datetime
    session_id: str | None
    task_id: str | None
    source: str                # "core", "tools.terminal", "integration.opencode"
    payload: dict              # typed per event type, JSON-serializable
```

Rules: immutable, JSON-serializable, validated against per-type payload
schemas. Consumers subscribe by type; delivery is in-order per emitter source.

## 3. Task Model

```python
@dataclass
class Task:
    id: str
    objective: str
    constraints: list[str]
    priority: int
    state: TaskState            # QUEUED RUNNING PAUSED COMPLETED FAILED CANCELLED
    steps: list[TaskStep]
    active_step: str | None
    dependencies: list[str]     # task ids
    owner: str                  # provider/agent id
    created_at: datetime
    updated_at: datetime
    retry_policy: RetryPolicy   # max_attempts, backoff, retryable_errors
    completion_criteria: list[VerificationCheck]
    metadata: dict

@dataclass
class VerificationCheck:
    kind: Literal["build", "test", "lint", "static", "manual", "custom"]
    command: str | None          # for tool-executed checks
    expected: str | None
    status: Literal["pending", "passed", "failed", "skipped"]
    evidence: list[str]          # logs/artifacts proving the outcome
```

**Persistence rule:** task state lives in SQLite, never only in RAM. Tasks
survive app restarts, provider failures, OpenCode disconnections, and network
failures. `updated_at` changes on every state transition.

## 4. Tool Contract

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON Schema
    output_schema: dict         # JSON Schema
    permission_class: str       # e.g. "filesystem.write"
    risk_level: RiskLevel       # READ LOW_WRITE HIGH_WRITE SYSTEM FORBIDDEN
    timeout_seconds: float
    cancellable: bool

class Tool(Protocol):
    spec: ToolSpec
    def validate(self, args: dict) -> None: ...
    def execute(self, args: dict, context: ToolContext) -> ToolResult: ...
    async def cancel(self) -> None: ...
```

`ToolContext` carries session, task, and an **approved-permissions token** —
tools refuse to run without the security layer's approval for their
`permission_class` + path/scope.

## 5. Security Interfaces

```python
class PermissionDecision(Enum):
    ALLOW = "allow"
    ASK = "ask"          # requires explicit user confirmation
    DENY = "deny"

class PermissionManager(Protocol):
    def decide(self, action: SecurityAction) -> PermissionDecision: ...
    def grant(self, decision: PermissionDecision, scope: str, ttl: float | None) -> PermissionGrant: ...
    def revoke(self, grant_id: str) -> None: ...
    def audit(self, entry: AuditEntry) -> None: ...   # append-only
```

All `HIGH_WRITE` / `SYSTEM` / previously-denied actions must be recorded in
the audit log with: timestamp, session, task, action, decision, user response.

## 6. Service Registry

```python
class ServiceRegistry(Protocol):
    def register(self, name: str, service: object, dependencies: list[str]) -> None: ...
    def resolve(self, name: str) -> object: ...
    def health(self) -> dict[str, HealthStatus]: ...   # per-service
    def start_all(self) -> None: ...
    def stop_all(self) -> None: ...
```

Startup order is derived from the dependency graph; shutdown is reverse order.

### Phase 1 implementation (`greatsage/core/registry.py`)

`ServiceRegistry` matches the contract with one deviation: services are
registered with **positional dependencies** (`register(name, service,
dependencies=("...",))`) and `resolve()` is `get()`-like; there is no
per-service `health()` yet — component health is centralized in the runtime's
health registry. `start_all()` topologically orders services (cycles and
missing dependencies are rejected at registration), calls `start()` if
present, and rolls back (reverse stop) the services already started if any
service fails to start.

## 7. Configuration Surface

```python
class Config(Protocol):
    def get(self, dotted_key: str, default: Any = MISSING) -> Any: ...
    def as_dict(self) -> dict: ...
    def validate(self) -> list[ConfigError]: ...
```

Validated on load against the documented schema (`docs/CONFIGURATION.md`).
Invalid config = refused startup, never silent fallback.

### Phase 1 implementation (`greatsage/configuration/`)

The Phase 0 dotted-get surface became executable as typed records instead:

```python
load_config(config_path: str | Path | None = None,
            environ: Mapping[str, str] | None = None) -> LoadedConfig
# LoadedConfig(config: JarvisConfig, source: str, config_path: Path | None)
```

- Precedence (low → high): built-in defaults → YAML file → `GREATSAGE_*`
  environment variables. CLI `--config PATH` selects the file; env
  `GREATSAGE_CONFIG_PATH` also selects it; otherwise `config/sage.yaml` in the
  repo root is used if present.
- `JarvisConfig` (frozen dataclasses: `core, logging, events, ai, memory,
  tasks, tools, security, voice`) is typed and immutable — raw dicts are
  never exposed after loading.
- `validate(raw)` reports every schema problem as
  `ConfigProblem(section, field, value, expected)`; missing provider fields,
  unknown fields/sections, wrong types, and out-of-range values are all
  refused. `apply_env` only recognizes schema-documented `GREATSAGE_SECTION__FIELD`
  variables (double underscore), e.g. `GREATSAGE_LOGGING__LEVEL`,
  `GREATSAGE_AI__PROVIDERS__OPENCODE__BASE_URL`, `GREATSAGE_SECURITY__DEFAULT_MODE`.
- Invalid configuration raises `ConfigurationError`; the CLI never starts the
  runtime on invalid config and never logs secret values.

The Phase 1/2 CLI surface (`greatsage` console script, `greatsage/cli.py`):

```text
greatsage --version                     → "greatsage 0.3.0", exit 0
greatsage --help
greatsage config validate [--config PATH]   → "Configuration valid." + "Source: …"
greatsage health [--config PATH]            → per-component + Overall health table
greatsage ai health [--config PATH] [--json]     → provider health (exit 1 if any unhealthy)
greatsage ai providers [--config PATH] [--json]  → registered providers + capabilities
greatsage ai benchmark [--config PATH] [--json]  → read-only hardware diagnostics
greatsage memory health [--config PATH] [--json] → memory subsystem health (exit 1 if unavailable)
greatsage memory stats [--config PATH] [--json]  → counts by type + subsystem state
greatsage memory list [--config PATH] [--json] [--content] [filters] [--limit N] [--offset N]
greatsage memory get ID [--config PATH] [--json] [--content] [--include-expired] [--include-deleted]
greatsage memory delete ID [--config PATH] [--json]    → auditable soft delete
greatsage memory delete [filters] --yes [--config PATH] [--json]  → bulk (needs a filter + --yes)
greatsage memory search QUERY [--config PATH] [--json] [--content] [--semantic] [filters]
# shared filters: --type, --source, --provenance, --min-confidence,
#                 --session, --include-expired, --include-deleted
greatsage memory digest [--days N] [--session SID] [--config PATH] [--json] [--content]
greatsage memory reindex [--limit N] [--config PATH] [--json]
greatsage tools list [--config PATH] [--json]                 → registered tools
greatsage tools info ID [--config PATH] [--json]              → one declaration
greatsage tools health [--config PATH] [--json]               → service + mode
greatsage tools execute ID [key=value ...] [--approve] [--json]
                        [--session-id SID] [--config PATH] → full pipeline
greatsage briefing [--days N] [--content] [--config PATH] [--json]
greatsage schedule add|list|remove|tick|health [--config PATH] [--json]
greatsage telegram health|listen [--once] [--for SECONDS] [--config PATH] [--json]
```

Exit codes: `0` success, `1` general failure (e.g. runtime failed to start,
memory not found, memory subsystem unavailable), `2` invalid
configuration/input.

## 8. Provider Health Contract

```python
@dataclass
class ProviderHealth:
    provider_id: str
    ok: bool
    latency_ms: float | None
    model_loaded: str | None
    detail: str | None
    last_check: datetime
```

The router uses this for failover decisions: unhealthy opencode → route
elsewhere or report, never hang indefinitely.

### Phase 2 implementation

`greatsage/intelligence/provider.py` implements exactly this shape as
`ProviderHealth` (`provider_id`, `ok`, `latency_ms`, `model_loaded`,
`detail`, `last_check`) plus a `to_dict()` for CLI/JSON output. `health()`
never raises: unreachable providers return `ok=False` with a detail string,
and stopped providers short-circuit before probing. The `intelligence`
runtime health check aggregates providers — HEALTHY when at least one
provider is healthy, UNHEALTHY when none are.

## 9. Memory Interfaces (Phase 3)

Implemented in `greatsage/memory/`; the service depends on the repository
abstraction, never on SQL.

```python
class MemoryType(StrEnum):
    WORKING = "working"          # session-scoped; RAM store, not persisted
    LONG_TERM = "long_term"      # stable facts/preferences, survives sessions
    EPISODIC = "episodic"        # records of J.A.R.V.I.S. actions
    SEMANTIC = "semantic"        # storage/retrieval foundation only (no ingestion yet)

@dataclass(frozen=True)
class Memory:
    id: str                      # "mem_<32 hex>", UUID-based, never sequential
    memory_type: MemoryType
    content: str | dict | list   # plain text (FTS-indexed) or JSON data
    source: str                  # where it came from (session, file, tool, user)
    provenance: str              # canonical kinds: user_explicit, system_event,
                                 # tool_result, document, agent_result, import
    confidence: float            # 0.0..1.0
    created_at: datetime         # timezone-aware UTC
    updated_at: datetime
    expires_at: datetime | None
    metadata: Mapping            # structured extras (JSON column)
    session_id: str | None
    deleted_at: datetime | None  # auditable soft delete

@dataclass(frozen=True)
class MemoryFilter:
    memory_type: MemoryType | None = None
    source: str | None = None
    provenance: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    expires_before: datetime | None = None
    minimum_confidence: float | None = None
    session_id: str | None = None
    include_expired: bool = False     # default: expired excluded
    include_deleted: bool = False     # default: deleted excluded
```

```python
class MemoryRepository(ABC):            # greatsage/memory/repository.py
    def initialize(self) -> None: ...   # create/migrate schema
    def create(self, memory: Memory) -> None: ...
    def get(self, memory_id, *, include_expired=False,
            include_deleted=False) -> Memory | None: ...
    def update(self, memory: Memory) -> None: ...   # atomic replace
    def delete(self, memory_id, deleted_at) -> bool: ...  # soft delete
    def list(self, filters: MemoryFilter) -> list[Memory]: ...  # created_at DESC, id ASC
    def search(self, query, filters: MemoryFilter) -> list[Memory]: ...
    def expire(self, now: datetime) -> list[str]: ...  # soft-deletes expired
    def count(self, filters: MemoryFilter) -> int: ...
    def stats(self) -> dict: ...
    def health(self) -> RepositoryHealth: ...
    def close(self) -> None: ...
```

`RepositoryHealth`: `accessible`, `schema_valid`, `migrations_current`,
`writable`, `fts_enabled`, `schema_version`, `detail`, `database_path`
(+ `to_dict()`).

```python
class MemoryService:                     # greatsage/memory/service.py
    availability: str                    # "healthy" | "disabled" | "unavailable"

    def remember(self, content, *, memory_type=LONG_TERM, source="",
                 provenance="user_explicit", confidence=None,
                 expires_at=None, metadata=None, session_id=None) -> Memory: ...
    def record_episode(self, *, content, action, source="", confidence=None,
                       expires_at=None, session_id=None) -> Memory: ...
    def add_semantic(self, *, content, source, provenance="document",
                     confidence=None, metadata=None) -> Memory: ...
    def retrieve(self, query=None, *, memory_type=None, source=None,
                 provenance=None, created_after=None, created_before=None,
                 expires_before=None, minimum_confidence=None,
                 session_id=None, include_expired=False,
                 include_deleted=False, limit=50, offset=0) -> MemoryRetrieval: ...
    def get(self, memory_id, *, include_expired=False,
            include_deleted=False) -> Memory: ...
    def update(self, memory_id, *, content=_MISSING, confidence=_MISSING,
               expires_at=_MISSING, metadata=_MISSING) -> Memory: ...
    def forget(self, memory_id) -> None: ...       # auditable soft delete
    def expire(self) -> int: ...                   # sweep → MemoryExpired events
    def working(self, session_id) -> WorkingMemory: ...
    def stats(self) -> dict: ...
    def health(self) -> dict: ...
```

`MemoryRetrieval` = `{items: list[RankedMemory], total: int}`;
`RankedMemory` = `{memory, score, match_reason}`. Ranking is deterministic:
`score = 0.5·relevance + 0.3·confidence + 0.2·recency` where recency =
`1/(1+age_days)`; a match reason (`"listed"` or `"query match: <query>"`)
accompanies every result.

`WorkingMemory` (per-session RAM): `add(content, ttl_seconds, item_id) -> id`,
`get(item_id)`, `remove(item_id)`, `list()` (active items, newest first),
`sweep()`, `clear()`. `WorkingMemoryStore.session(session_id)` returns the
session's store; sessions never share items.

## 10. Tool System Interfaces (Phase 4)

Implemented in `greatsage/tools/`; every execution — AI or CLI — goes through
`ToolService.execute` (no bypass). Full contract: `docs/TOOLS.md`.

```python
class ToolRisk(StrEnum):     # safe | low | medium | high | critical
class ToolCategory(StrEnum): # filesystem | process | system | shell
                             # network | browser | gui (reserved)
class ToolDecision(StrEnum): # allow | ask | deny  (never booleans)
class ApprovalOutcome(StrEnum):  # approved | denied | timeout | cancelled

@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    tool_id: str
    arguments: dict = {}
    source: str = "ai"              # "ai" | "cli"
    session_id: str | None = None
    task_id: str | None = None

@dataclass(frozen=True)
class ToolContext:              # built by the service from config
    working_directory: Path
    environment: dict[str, str] # scrubbed — no JARVIS secrets
    timeout_seconds: float
    max_output_bytes: int

@dataclass(frozen=True)
class ToolResult:
    request_id: str
    tool_id: str
    success: bool
    output: Any = None
    error: str | None = None
    metadata: dict = {}
    duration_ms: float = 0.0

class BaseTool:
    id: str                       # e.g. "filesystem.read"
    name: str
    description: str
    version: str
    risk_level: ToolRisk
    category: ToolCategory
    capabilities: tuple[str, ...]
    input_schema: dict            # JSON-schema subset (object/string/…)
    output_schema: dict
    PATH_ARGUMENTS: tuple[str, ...] = ()   # path-checked argument names
    def execute(self, args: dict, context: ToolContext) -> ToolResult: ...
```

```python
class ToolRegistry:                     # greatsage/tools/registry.py
    def register(self, tool: BaseTool) -> None: ...   # dup/id/schema rejected
    def get(self, tool_id: str) -> BaseTool: ...      # ToolNotFoundError
    def list_ids(self) -> tuple[str, ...]: ...
    def describe(self, tool_id: str) -> dict: ...     # full declaration
    def health(self) -> dict: ...                     # status/tool_count/tools

class SecurityPolicy:                   # greatsage/tools/policy.py
    @classmethod
    def from_config(cls, config: JarvisConfig) -> SecurityPolicy: ...
    def evaluate(self, request, tool) -> tuple[ToolDecision, str]: ...

class ToolService:                      # greatsage/tools/service.py
    availability: str                   # "healthy" | "disabled" | "unavailable"
    approval: ApprovalProvider | None   # public; install to answer ASK
    publisher: Any                      # callable(Event); wired by the runtime
    def start(self, config: JarvisConfig | None = None) -> None: ...
    def execute(self, request: ToolRequest) -> ToolResult: ...
    def registry(self) -> ToolRegistry: ...
    def health(self) -> dict: ...
    def register_health_check(self, health_registry: HealthRegistry) -> None: ...

class ApprovalProvider(ABC):            # greatsage/tools/approval.py
    def request_approval(self, request: ToolRequest, tool: BaseTool,
                         reason: str) -> ApprovalOutcome: ...
```

Pipeline semantics: policy decisions are `ALLOW`/`ASK`/`DENY`; `ASK` without
a provider is DENY (fail-closed); `critical` is DENY in every mode; hooks
(path roots + protected files, shell classifier, sensitive arguments) only
tighten. Results are capped at `tools.max_output_bytes` with a truncation
marker, and every attempt is published as a `TOOL_*` event carrying
`request_id`/`tool_id`/`risk_level`/`session_id`/`task_id`.
# 11. Agent System Interfaces (Phase 5A)

Authority hierarchy: **AI ≠ authority; Tool Security = authority; Agent
Orchestrator = control flow**. Interface contracts below; see
docs/AGENTS.md for the operating rules.

```python
# greatsage/agent/models.py (provider-neutral; never OpenCode-specific)

class AgentState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    EXECUTING_TOOL = "executing_tool"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LIMIT_REACHED = "limit_reached"

class ToolCall:
    id: str
    tool_id: str            # registry id, e.g. "filesystem.read"
    arguments: dict         # structured arguments only
    sequence: int           # 0-based step index

class ToolCallResult:
    call: ToolCall
    success: bool
    output: Any | None
    error: str | None
    duration_ms: float
    state: AgentState | None = None   # terminal/cancelled marker

class AgentResult:
    task_id: str
    state: AgentState
    steps: int
    tool_calls: int
    elapsed_ms: float
    provider: str | None
    model: str | None
    final_text: str | None
    error: str | None
    reason: str | None
    request_id: str
    started_at: str
    finished_at: str | None
    memory_references: int = 0
    session_id: str | None = None
    def to_dict(self) -> dict: ...    # JSON-safe, no prompts, no secrets
```

```python
class AgentLimits:          # greatsage/agent/limits.py (frozen dataclass)
    max_steps: int
    max_tool_calls: int
    max_wall_time_seconds: float
    max_single_tool_calls: int
    max_total_tool_output_bytes: int
    loop_detection_threshold: int

class AgentOrchestrator:    # greatsage/agent/orchestrator.py
    def run(self, task: AgentTask, *, limits: AgentLimits,
            tools: ToolService, intelligence: IntelligenceService,
            memory: MemoryService | None, cancel_token: CancellationToken,
            publisher: Callable[[Event], None]) -> AgentResult: ...

class AgentService:         # greatsage/agent/service.py
    availability: str       # "healthy" | "disabled" | "unavailable"
    def start(self, config: JarvisConfig) -> None: ...
    def run(self, prompt: str, *, session_id: str | None = None,
            provider: str | None = None, model: str | None = None,
            max_steps: int | None = None) -> AgentResult: ...
    def cancel(self, task_id: str) -> None: ...
    def health(self) -> dict: ...   # available/status/enabled/detail/current
    def register_health_check(self, health_registry) -> None: ...

class AgentApprovalProvider:   # greatsage/agent/approval.py
    def approve_all(self) -> None: ...
    def disapprove_all(self) -> None: ...
    def is_open(self) -> bool: ...
    def request_approval(self, request, tool, reason) -> ApprovalOutcome: ...
```

### Loop semantics

- Strictly sequential tool calls — one per step, no parallel calls.
- `AgentService._require_tool_calling` gates on provider registry wiring,
  owned provider, `TOOL_CALLING` capability, `READY` state → typed errors,
  never silent fallback.
- Every tool call flows through `ToolService.execute` (the security
  pipeline) — the service never calls a tool directly.
- Approval: `AgentService` swaps in an `AgentApprovalProvider` for the run
  and restores the previous provider afterwards; `ASK`-gated calls pause
  the state machine in `WAITING_FOR_APPROVAL`.
- Hard limits are enforced against ceilings at the start of every run
  (smaller limit wins); exhaustion ends the run `LIMIT_REACHED` with the
  exact `reason`.
- Cooperative cancellation: `cancel(task_id)` flips the token; checked
  between steps and before each tool call; approval gates also resolve to
  cancelled.

### CLI

```text
greatsage agent health [--config PATH] [--json]
greatsage agent run --prompt TEXT [--session-id SID] [--provider NAME]
                  [--model NAME] [--max-steps N] [--json] [--config PATH]
```

Exit codes: `0` completed run / healthy; `1` failure (unavailable provider,
runtime failure, non-completed state); `2` invalid prompt/configuration.

## 12. Workspace / Planning / Task Interfaces (Phase 6)

Deterministic, offline, stdlib-only. Plans never call an AI provider;
tasks execute plan steps exclusively through the Phase 4 tool pipeline
(same allow/ask/deny + audit semantics as `tools execute`).

```python
class WorkspaceService:   # greatsage/workspace/service.py
    def scan(self, root: str | None = None) -> dict: ...
    def info(self, workspace_id: str | None = None,
             root: str | None = None) -> dict | None: ...
    def health(self) -> dict: ...   # available/status/enabled/detail

class PlanningService:    # greatsage/planning/service.py
    def create_plan(self, goal: str, workspace: Any = None,
                    plan_id: str | None = None) -> dict: ...
    def get(self, plan_id: str) -> dict | None: ...
    def list(self) -> list[dict]: ...
    def health(self) -> dict: ...   # available/status/enabled/detail

class TaskService:        # greatsage/task/service.py
    def run(self, plan: str) -> TaskReport: ...     # plan file (.yaml/.json) or plan_id
    def resume(self, task_id: str) -> TaskReport: ...
    def list(self, limit: int = 50) -> list[dict]: ...
    def get(self, task_id: str) -> dict: ...
    def cancel(self, task_id: str) -> dict: ...
    def health(self) -> dict: ...   # available/status/enabled/detail
```

### CLI

```text
greatsage workspace scan [PATH] [--config PATH] [--json]
greatsage workspace info [--id ID] [--path PATH] [--config PATH] [--json]
greatsage workspace health [--config PATH] [--json]
greatsage planning create GOAL [--workspace-id ID] [--workspace-root PATH]
                     [--plan-id ID] [--config PATH] [--json]
greatsage planning get <plan_id> [--config PATH] [--json]
greatsage planning list [--config PATH] [--json]
greatsage planning health [--config PATH] [--json]
greatsage task run PLAN [--config PATH] [--json]
greatsage task resume <task_id> [--config PATH] [--json]
greatsage task list [--limit N] [--config PATH] [--json]
greatsage task get <task_id> [--config PATH] [--json]
greatsage task cancel <task_id> [--config PATH] [--json]
greatsage task health [--config PATH] [--json]
```

Exit codes: `0` success / healthy / completed; `1` not-found, unavailable
subsystem, or non-completed task state; `2` invalid input/configuration
(empty goal/plan id, scan outside allowed roots, malformed plan file).

### Phases 7–10 status (integration notes)

- Phase 8 (vision) and Phase 10 (HUD) CLIs are merged
  (`greatsage vision health|capture|describe`, `greatsage hud|status|dashboard`);
  vision has no `vision.*` config section yet — the service is
  config-independent (see merge-risk notes in the Phase 6–10 QA report).
- Phase 6 voice pipeline CLI is merged (`greatsage voice health|listen|speak`).
- Phase 7 (autonomy: planner + task-graph + verification on top of
  `greatsage/planning` + `greatsage/task`) and Phase 9 (security hardening)
  are implemented (`greatsage/planning/graph.py` + `verify.py`,
  `greatsage/tools/redaction.py` + policy tightening — see `docs/AUTONOMY.md`
  and `docs/SECURITY_MODEL.md` §10).
