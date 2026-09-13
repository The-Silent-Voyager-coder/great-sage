# Configuration System

## 1. Principles

- **Externalized, never hard-coded.** Model names, paths, ports, OpenCode
  endpoint, voice engines, permissions, storage locations, resource limits —
  all come from configuration.
- **Validated.** Invalid configuration refuses startup; no silent fallbacks.
- **Layered.** Defaults → config file (`config/sage.yaml`) → environment
  variables → CLI overrides. Later layers win.
- **Secrets in env, never in files.** API keys/tokens live in `.env`
  (git-ignored) or the process environment. Never in YAML, never in source.

## 2. Sources (priority low → high)

| Layer | Location | Use |
|---|---|---|
| 1. Built-in defaults | `configuration/` package | sane minimums |
| 2. User config | `config/sage.yaml` (copy of `sage.example.yaml`) | normal settings |
| 3. Environment | `GREATSAGE_*` variables, `.env` file | machine-specific + secrets |
| 4. CLI flags | `greatsage --...` | per-run overrides |

Convention: env var for config key `security.default_mode` is
`GREATSAGE_SECURITY__DEFAULT_MODE` (double underscore = nesting).

## 3. Schema (authoritative — `config/sage.example.yaml`)

Top-level sections:

| Key | Purpose |
|---|---|
| `core.*` | identity, data/cache/logs/runtime/workspaces/models/backups dirs, timezone |
| `logging.*` | level, format, retention |
| `events.*` | bus queue size, worker count |
| `ai.*` | default provider, provider registry (local/opencode), endpoints, timeouts |
| `memory.*` | SQLite path, enable flag, auto-save policy, default confidence, retention, embeddings (model/base_url/enable) |
| `tasks.*` | max iterations, default timeout, persist interval |
| `task.*` | bounded multi-step execution limits + database path |
| `workspace.*` / `planning.*` | discovery scanner / deterministic planner limits + database paths |
| `scheduler.*` | enable flag, max schedules ceiling-guarded, database path |
| `telegram.*` | enable flag, token env name, allowlisted chat ids, poll/listen bounds |
| `tools.*` | working directory, execution timeout, output cap, allowed/denied roots, per-category default risk |
| `security.*` | mode (`normal`/`lockdown`/`development`), auto-approve rules, audit log path |
| `voice.*` | wake word / STT / TTS engine + model selections (mock default; vosk/piper/fuzzy opt-in) |

Unknown keys or invalid values → validation errors at startup.

## 4. Secrets Policy

- `.env` / `.env.*` are git-ignored (`.gitignore` enforces this).
- `.env.example` documents variable names only.
- API keys are referenced as `*_env` names, e.g.
  `ai.providers.opencode.api_key_env: "OPENCODE_API_KEY"`; the config loader
  reads the environment at runtime.
- If a configured secret env var is unset and the feature requires it, the
  feature is disabled with a clear log — the core never guesses or fabricates
  credentials.

## 5. Default Paths

Defaults assume `C:\GREATSAGE\` as the data root (config override supported):

```text
C:\GREATSAGE\data\         SQLite DBs (memory, tasks), audit log
C:\GREATSAGE\models\       local models (Ollama files, whisper, piper voices)
C:\GREATSAGE\workspaces\   active dev workspaces
C:\GREATSAGE\cache\        transient caches
C:\GREATSAGE\logs\         structured logs
C:\GREATSAGE\runtime\      pid/socket/ephemeral
C:\GREATSAGE\backups\      archives for Google Drive
```

## 6. Implementation Notes (Phase 1)

Phase 1 ships the complete loader, validator, and CLI:

- **Loader** (`greatsage/configuration/loader.py`): deep-merges built-in defaults
  (`defaults.py`, mirroring `config/sage.example.yaml`) → YAML file (selected
  by `--config PATH` or `GREATSAGE_CONFIG_PATH`, else `config/sage.yaml` if
  present) → `GREATSAGE_*` environment variables → validates → freezes into typed
  records.
- **Validation** (`greatsage/configuration/validation.py`): schema-driven; every
  problem is reported as `section.field = value, Expected: …`; unknown
  sections/fields, wrong types, bad enums, non-absolute paths, invalid
  http(s) URLs (port 1–65535), and missing provider fields are refused with
  `ConfigurationError` — no silent fallbacks, no secrets in messages.
- **Typed config** (`greatsage/configuration/model.py`): frozen dataclasses
  (`JarvisConfig` + per-section records, `SecurityMode`/`RiskLevel` StrEnums);
  raw dicts never escape the loader.
- **Env vars**: only schema-documented keys are recognized
  (`GREATSAGE_SECTION__FIELD`, `GREATSAGE_AI__PROVIDERS__<NAME>__<FIELD>`);
  unknown `GREATSAGE_*` keys are ignored, and unparseable values fail with a
  `ConfigurationError` naming the variable.
- **CLI**: `greatsage config validate [--config PATH]` prints `Configuration
  valid.` + `Source:` (resolved path or `built-in defaults`), exits `0`/`2`.
- The `Config` object is exposed via the runtime: `Runtime.config
  (greatsage.configuration.model.JarvisConfig)`.

## 7. AI Provider Configuration (Phase 2)

The `ai.*` schema drives the intelligence layer (`greatsage.intelligence`):

```yaml
ai:
  default_provider: local                      # explicit selection when no
                                               # hint in the request
  providers:
    local:
      type: local                              # Ollama adapter
      enabled: true
      base_url: http://127.0.0.1:11434
      model: ""                                # provider decides
      timeout_seconds: 60
    opencode:
      type: opencode
      enabled: false                           # off by default
      base_url: http://127.0.0.1:4096
      api_key_env: ""                          # e.g. "OPENCODE_API_KEY"
      timeout_seconds: 120
```

Semantics:

- `providers.<name>.type` is an enum: `local` (Ollama adapter) or `opencode`
  (remote). Unknown or disabled types are not registered.
- `base_url` must be a valid http(s) URL (validated with port range).
- `api_key_env` names an environment variable; the value is read once at
  start and used for Bearer auth. Never put the key itself in YAML.
- `model` is the default model hint for the provider; the provider decides
  when empty (`/api/tags` for Ollama) and the router drops providers whose
  advertised models exclude an explicit request model.
- The default config (`config/sage.example.yaml`, `configuration/defaults.py`)
  enables the local provider only; opencode is disabled until the operator
  flips it on. A missing `ai:` section is valid — defaults apply.
- `default_provider` is a fallback, not a pin: explicit request metadata
  (`metadata.provider`) wins, and an explicitly selected provider that is
  unhealthy is a routing error — never a silent switch to another provider.

CLI: `greatsage ai health|providers|benchmark [--config PATH] [--json]`.
`ai health` exits `1` when any provider is unhealthy (or both are
unavailable — the CLI survives and reports), `2` on config errors.

## 8. Memory Configuration (Phase 3)

The `memory.*` schema drives the memory subsystem (`greatsage.memory`):

```yaml
memory:
  enabled: true                          # false → subsystem disabled (health: healthy no-op)
  database_path: C:/GREATSAGE/data/sage-memory.db
  auto_save_conversations: false         # ALWAYS false in Phase 3 (privacy rule)
  default_confidence: 0.8                # 0.0..1.0 when remember() omits confidence
  retention_days: 365                    # default expiry for working/episodic entries
```

Semantics:

- `enabled` gates the whole subsystem. When `false`, `greatsage health` reports
  the memory component as HEALTHY (a deliberate no-op, not an error), memory
  commands fail cleanly with an "unavailable" message, and no database file
  is created.
- `database_path` is the SQLite file; the parent directory is created on
  first use. Must be an absolute path.
- `auto_save_conversations` exists in the schema but is **forbidden to
  enable** in Phase 3: memory writes require a deliberate save decision
  (`remember()`). Validation enforces `false`.
- `default_confidence` is the confidence applied when a `remember()` call
  does not specify one; values outside 0.0–1.0 are refused.
- `retention_days` sets the default expiration for WORKING and EPISODIC
  memories created without an explicit `expires_at`. LONG_TERM and SEMANTIC
  memories are persistent unless an explicit expiration is given.

Environment overrides use the double-underscore convention:
`GREATSAGE_MEMORY__DATABASE_PATH`, `GREATSAGE_MEMORY__DEFAULT_CONFIDENCE`,
`GREATSAGE_MEMORY__RETENTION_DAYS`.

CLI: `greatsage memory health|stats|list|get|delete|search [--config PATH]
[--json]`. `memory health` exits `1` when the subsystem is unavailable
(e.g. corrupted database — the file is kept as-is), `2` on config errors.

## 9. Tool Security Configuration (Phase 4)

The `security.*` + `tools.*` schema drives the tool system (`greatsage.tools`):

```yaml
security:
  mode: normal                        # normal | lockdown | development
  allow_auto_approve_read: true       # development mode: `low` risk allowed
tools:
  working_directory: C:/GREATSAGE/workspaces   # explicit cwd for every tool
  execution_timeout_seconds: 30.0           # per-run subprocess timeout
  max_output_bytes: 65536                   # output truncation bound
  allowed_roots: [C:/GREATSAGE/workspaces]     # path policy applies here
  denied_roots: []                          # explicit denials win
  terminal:
    default_risk: LOW_WRITE                 # base risk for shell.execute
  browser:
    default_risk: READ                      # reserved category
```

Semantics:

- `security.mode` selects the policy matrix: `normal` (safe → allow; low →
  allow when `allow_auto_approve_read` is true — the default — else ask;
  medium/high → ask; critical → deny everywhere), `lockdown` (only `safe`
  tools), `development` (as `normal`, plus `medium` tools allowed).
- `tools.working_directory` is the explicit working directory for every
  tool; `execution_timeout_seconds` bounds every subprocess; output above
  `max_output_bytes` is replaced by a truncation marker.
- `allowed_roots`/`denied_roots` feed the path security hook: reads/writes
  outside the roots are denied; denied roots are denied even when listed as
  allowed. Protected files (`memory.db`, `.env`, secret-stemmed names) are
  always denied.
- `tools.<category>.default_risk` sets the base risk for a category's tools
  (the shell classifier can only raise it).

Environment overrides use the double-underscore convention:
`GREATSAGE_SECURITY__MODE`, `GREATSAGE_TOOLS__ALLOWED_ROOTS`,
`GREATSAGE_TOOLS__EXECUTION_TIMEOUT_SECONDS`.

CLI: `greatsage tools list|info|health|execute [--config PATH] [--json]
[--approve]`. See `docs/TOOLS.md` for the full tool-system contract.
## 10. Agent Section (Phase 5A)

| Key | Default | Ceiling | Purpose |
|---|---|---|---|
| `agent.enabled` | `true` | – | enable the agent service and `greatsage agent` commands |
| `agent.max_steps` | `12` | `25` | maximum generate steps per run |
| `agent.max_tool_calls` | `8` | `50` | maximum tool calls per run |
| `agent.max_wall_time_seconds` | `300` | `1800` | wall-clock budget per run |
| `agent.max_single_tool_calls` | `3` | `10` | repeat budget for one identical tool call |
| `agent.max_total_tool_output_bytes` | `2097152` (2 MiB) | `16777216` | cumulative tool output cap per run |
| `agent.loop_detection_threshold` | `3` | `25` | exact-match tool-call signature bursts |

Rules:

- Limits are validated with hard ceilings in `greatsage/configuration/validation.py`
  (via `greatsage/agent/limits.py`); configuration above a ceiling is refused
  at startup.
- Run-time overrides (`--max-steps`) are clamped: the smaller applicable
  limit wins (config or ceiling).
- `--max-steps N` on `greatsage agent run` overrides the configured step limit
  for that run only, still clamped to the ceiling.

Environment override convention: `GREATSAGE_AGENT__MAX_STEPS`,
`GREATSAGE_AGENT__ENABLED`.

CLI: `greatsage agent health|run [--config PATH] [--json]`. See
`docs/AGENTS.md` for the full agent-system contract.
## 11. Workspace / Planning / Task Sections (Phase 6)

```yaml
workspace:
  enabled: true
  max_scan_depth: 3
  max_entries: 500
  scan_timeout_seconds: 10.0
  database_path: C:/GREATSAGE/data/sage-workspace.db
planning:
  enabled: true
  max_plan_steps: 25
  database_path: C:/GREATSAGE/data/sage-plans.db
task:
  enabled: true
  max_steps: 25
  per_step_timeout_seconds: 30.0
  total_timeout_seconds: 600.0
  database_path: C:/GREATSAGE/data/sage-tasks.db
```

Rules:

- Limits are validated with hard ceilings in `greatsage/configuration/
  validation.py` (via `greatsage/workspace/limits.py`,
  `greatsage/planning/limits.py`, `greatsage/task/limits.py`); configuration
  above a ceiling is refused at startup.
- `enabled: false` gates the whole subsystem (healthy no-op in
  `greatsage health`; commands fail cleanly with an "unavailable" message).

Environment override convention: `GREATSAGE_WORKSPACE__MAX_ENTRIES`,
`GREATSAGE_PLANNING__MAX_PLAN_STEPS`, `GREATSAGE_TASK__MAX_STEPS`.

CLI: `greatsage workspace scan|info|health`, `greatsage planning
create|get|list|health`, `greatsage task run|resume|list|get|cancel|health`
(see `docs/INTERFACES.md` §12).

Note: the Phase 6 voice pipeline adds a `voice:` section (wake_word / stt
/ tts engine + model selections, see `config/sage.example.yaml`).
Vision (Phase 8) currently defines no `vision.*` config keys — its
service is config-independent. Phase 7 (autonomy) and Phase 9 (security)
add no new config sections by design: Phase 7 ships as
`greatsage/planning/graph.py` + `verify.py` over the Phase 6 `planning:` /
`task:` blocks, and Phase 9 ships as `tools/redaction.py` + policy
tightening under the existing `security:` / `tools:` blocks.

## 12. Scheduler / Telegram Sections (roadmap)

```yaml
scheduler:
  enabled: true
  max_schedules: 50          # ceiling 200, refused above it
  database_path: C:/GREATSAGE/data/sage-scheduler.db
telegram:
  enabled: false             # flip on after BotFather + allowlist (see below)
  token_env: "TELEGRAM_BOT_TOKEN"
  allowed_chat_ids: []       # your chat id(s) only; strangers get no reply
  poll_timeout_seconds: 20   # ceiling 50
  max_listen_seconds: 600    # ceiling 3600
```

Rules: `scheduler` follows the standard subsystem pattern (enabled gate +
ceiling validation). `telegram` additionally fails `health` as unavailable
until a token is present in the named env var AND at least one chat is
allowlisted. The token never appears in files, logs, or events.

CLI: `greatsage schedule add|list|remove|tick|health`,
`greatsage telegram health|listen [--once] [--for SECONDS]`.

## 13. Memory Embeddings (roadmap Phase B)

```yaml
memory:
  # ... base keys as in §8 ...
  embeddings_enabled: false
  embedding_model: "nomic-embed-text"
  embedding_base_url: "http://127.0.0.1:11434"
```

Rules: off by default; enabling requires the embedding model pulled once
(`ollama pull nomic-embed-text`). Saves auto-embed best-effort (backend
down = save stands, vector missing); explicit `--semantic` search fails
loudly instead of falling back. Vectors live in the local
`memory_embeddings` table and die with their memory (`forget`/`delete`).
CLI: `greatsage memory search QUERY --semantic`, `greatsage memory reindex`.
