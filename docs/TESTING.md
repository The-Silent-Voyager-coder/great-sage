# Testing Strategy

## 1. Principles

- Every module requires tests; new code without tests is not merged.
- Dangerous computer-control operations require **explicit safety tests**
  (deny paths, scope checks, audit entries, timeouts).
- Tests must run locally, offline, without paid services. External services
  are mocked at their boundary (provider adapter interfaces).
- Tests must not require administrator privileges or touch real user data.

## 2. Test Layers (directories under `tests/`)

| Layer | Directory | Covers | Real services? |
|---|---|---|---|
| Unit | `tests/unit/` | logic of each module in isolation | No |
| Integration | `tests/integration/` | module-to-module (core ↔ events ↔ config ↔ storage) | Local SQLite only |
| Provider | `tests/providers/` | AIProvider contract conformance, model router behavior | Mocked HTTP servers |
| Tools | `tests/tools/` | each tool: validation, execution, permission enforcement, cancel | Temp dirs only |
| Security | `tests/security/` | risk classification, allow/ask/deny, audit log integrity, TTL expiry, denied-action-retry blocking | No |
| Memory | `tests/memory/` | memory types, provenance, retrieval, expiration, no-auto-save guarantee | Temp SQLite |
| Task lifecycle | `tests/tasks/` | full task state machine + restart persistence | Temp SQLite |
| OpenCode | `tests/opencode/` | client against a **mock OpenCode server** implementing the documented endpoints (OpenAPI 3.1 fixture); permission-request flow, abort, SSE reconnection | Mock server |

## 3. Required Coverage for Every New Module

- happy path
- validation failures (invalid args/schema)
- timeout behavior
- cancellation
- error propagation (typed exceptions, no swallowed failures)
- permission paths: allow / ask / deny, scope mismatch, expired grant
- audit logging present on every risky action

## 4. Safety Test Examples (security layer)

- `HIGH_WRITE` delete outside trusted root → DENY + audit entry, file intact
- grant TTL expiry → expired grant refused
- agent retry of a denied action → blocked, second audit entry
- terminate tool on timeout → child process killed, no zombie processes
- config with invalid schema → refuses startup

## 5. Verification Commands

```text
pytest                     # full suite (dev extra)
pytest -m safety           # safety subset — must pass before any release
pytest tests/opencode      # mock-server OpenCode integration
pytest tests/unit/intelligence   # Phase 2 provider suite
pytest --cov=greatsage --cov-report=term-missing
ruff check .               # linter
mypy greatsage                # type checker (dev extra)
```

Target: ≥80% coverage on `greatsage/` modules; 100% on `security/` decision paths.

## 6. Phase 1 baseline

- 95 tests across `tests/unit/configuration|events|core|observability` and
  `tests/integration/test_cli.py`; coverage 93% (`greatsage/`).
- Key behaviors proven by tests: config precedence + env override + provider
  repair-by-defaults; strict validation output; event ordering, filtering and
  subscriber-failure isolation; topological registry start/stop with rollback;
  lifecycle transitions incl. startup-failure cleanup; idempotent shutdown;
  health aggregation; secret redaction and correlation-ID inheritance in JSON
  logs; CLI help/version/validate/health and exit codes 0/1/2.

## 6a. Phase 2 baseline (intelligence layer)

- 199 tests total (95 → 199). New suites under `tests/unit/intelligence/`:
  models (validation + serialization), provider registry (register/get/
  health/all-states), mock provider (deterministic output, streaming,
  configurable failure/latency), router (10 scenarios, 100% coverage —
  explicit selection never falls back, capability/availability/model filters,
  coding→CODE_EXECUTION preference, local-only preference), Ollama + OpenCode
  adapters against an in-process fake HTTP server (`conftest.py` →
  `ThreadingHTTPServer` with scripted routes), benchmark, and the
  IntelligenceService facade (events, failure isolation, health registration).
  CLI integration tests added: `ai health|providers|benchmark` incl. `--json`
  and exit codes (0 healthy, 1 any-unhealthy, 2 config error).
- **No real services in tests**: fake servers only; no internet, no API keys,
  no model downloads. Mock provider is born READY so unit tests never touch
  the network.
- Provider failure isolation is a concrete test: both adapters pointed at a
  dead endpoint produce UNAVAILABLE health and the service/CLI survive.

## 6b. Phase 3 baseline (memory layer)

- 314 tests total (199 → 314). New suites:

| Directory | Covers |
|---|---|
| `tests/unit/memory/test_memory_models.py` | type/provenance/content/confidence validation invariants, id format, `to_dict` redaction, expiry checks, filter validation |
| `tests/unit/memory/test_memory_repository.py` | schema init/health (30+ migrations, versioning, newer-schema refusal, corrupted DB kept as-is), CRUD round-trips incl. structured content, soft delete, list ordering/filters, FTS5 phrase/case/source matching, LIKE fallback, expire sweep, stats, transactional rollback (duplicate id) |
| `tests/unit/memory/test_memory_working.py` | default TTL, no-expiry (`ttl≤0`), lazy purge, sweep, newest-first ordering, remove/clear, strict session isolation |
| `tests/unit/memory/test_memory_service.py` | recording publisher fixture; start healthy/disabled/unavailable; remember defaults (config `default_confidence`) + validation; record_episode/add_semantic; ranking order + limit/offset totals; search incl. structured content; expired/deleted exclusion + explicit inclusion; update immutability + not-found; forget soft delete; expire events; working memory via service; stats/health; publisher failure swallowed; **events never carry content** (regression-tested) |

- `tests/integration/test_cli.py` gained the full memory CLI matrix: health
  (text/JSON/invalid config), stats, list (empty/JSON/filtered), get/delete
  unknown id (exit 1), delete without filters (exit 2), bulk delete without
  `--yes` (exit 2), full CRUD round-trip, filter-based bulk delete with
  `--yes`, JSON redaction of content without `--content`.
- `tests/unit/configuration/test_validation.py` gained the
  `auto_save_conversations: true` refusal (privacy rule).
- Failure isolation and corruption paths run against real temp SQLite files
  (no mocks): corrupted file → `unavailable` + file intact; unwritable parent
  dir; closed repository operations; transaction rollback leaves no partial
  rows.
- Memory tests are fully offline: no Ollama, no OpenCode, no API keys.

## 6c. Phase 4 baseline (tool system)

- **531 tests total (314 → 531)** — 217 new tests, all offline. New suites under
  `tests/unit/tools/` (+ shared `stub_tools.py`) and `tests/integration/`
  `test_cli_tools.py`:

| File | Covers |
|---|---|
| `test_tool_models.py` | schema/argument validation (object/string/integer/number/boolean/array, required, bounds, array items), enum coverage — decisions never boolean, 5 risks, approval outcomes, reserved categories |
| `test_tool_registry.py` | register/get/duplicate-rejected/non-Tool-rejected/invalid-schema/unregister/describe/health |
| `test_shell_classifier.py` | parametrized SAFE/RESTRICTED/DANGEROUS/FORBIDDEN command tables, empty command, git destructive, case/`.exe` insensitivity |
| `test_pathsecurity.py` | canonicalize (absolute/relative/`~`), is_within incl. prefix-sibling negatives, protected files, secret stems, directories never protected |
| `test_environment.py` | scrub allowlist, marker tokens incl. plurals, merge rejections (non-string values, secret keys) |
| `test_policy.py` | mode matrix (LOCKDOWN/NORMAL/DEVELOPMENT/CRITICAL-in-all-modes), `allow_auto_approve_read`, the three hooks, hooks-only-tighten, disjoint classifier tables, denial reasons |
| `test_filesystem_tools.py` | list/stat/read (binary refusal, truncation)/mkdir/write (atomic, overwrite, non-atomic, oversize, missing parent), risk pins, **no delete tool**, list-entry cap |
| `test_process_tools.py` | list contains current pid, info current/unknown/negative, **no terminate tool**, `tasklist` smoke test |
| `test_shell_tools.py` | success/exit-code failure/timeout/truncation/env additions/secret-env rejection/non-string env/cwd enforce + missing cwd/unstartable binary/duration/output fields |
| `test_system_tools.py` | `system.info` shape + environment-secret redaction through serialization |
| `test_tool_service.py` | full pipeline event sequences (allowed/asked/denied/rejected/approved), approval override, denial reasons, output truncation, health, publisher-never-raises, no sensitive args in events, medium/low/allow_low matrix, no delete/kill in registry |
| `test_cli_tools.py` (integration) | `tools list|info|health|execute` incl. `--json`, invalid args (exit 2), unknown tool (exit 2), medium write without/with `--approve`, shell without/with `--approve`, dangerous command denied, path outside roots denied, session-id propagation |

- Key behaviors proven end-to-end through the real CLI: **approval is the
  only way through `ASK`** (medium write / shell without `--approve` →
  "no approval provider configured", exit 1), **policy denials hold even
  with `--approve`** (dangerous command, path outside allowed roots),
  `system.info` executes without approval (safe).
- Implementation defects caught by the suite: plural secret markers
  (`credentials`) missed by `is_secret_name`; a Windows `ctypes`
  `GetDiskFreeSpaceW` call crashing the interpreter inside `system.info`
  (replaced with stdlib `shutil.disk_usage`, crash-proof); policy tests
  initially used paths inside allowed roots (now genuinely outside).
- No `shell=True`, no network, no real services: subprocess tests run
  `sys.executable --version`; `tasklist` only in one guarded smoke test.

## 7. CI (later)

Phase 1+ adds a local pre-commit hook or GitHub Actions (free tier) running
lint (ruff), type checks (mypy), and the suite. Zero-cost constraint applies:
no paid CI services.

## 8. Rules

- Never weaken a test to make a build pass.
- Never mark a failing test "skipped" without a tracked issue + reason.
- A task is only COMPLETED when its verification steps actually passed —
  tests are the primary evidence (`VerificationCheck.evidence`).
## 9. Agent Suite (Phase 5A)

Location: `tests/unit/agent/` (unit), `tests/integration/test_cli_agent.py`
(CLI wiring), scripted tool-call tests in `tests/unit/intelligence/test_mock.py`.

Coverage:

- **State machine**: every transition validated; invalid transitions raise.
- **Approval**: pause in `WAITING_FOR_APPROVAL`, approve/deny resolution,
  denial fed back to the LLM (no auto-retry), approval swap restored after
  the run, `disapprove_all` fail-closed, timeout path, cancellation of a
  pending gate.
- **Bounded loop**: `max_steps`, `max_tool_calls`, wall-clock, per-tool
  repeat count, cumulative output bytes, loop detection — each ends
  `LIMIT_REACHED`/`TIMED_OUT` with the exact reason.
- **Provider gating**: unregistered/unavailable provider, missing
  `TOOL_CALLING` capability → typed errors, no silent fallback.
- **Cancellation**: between steps, mid-tool-call (threaded test), before
  any tool call; no orphaned state.
- **No-bypass proof**: tools are reachable only through the fake
  `ToolService` pipeline; the security pipeline tests (`tests/tools/`,
  `tests/security/`) keep asserting deny paths.
- **Events**: exact `AGENT_*` order for one tool call (started → step →
  request → completed → step → step → completed → agent_completed);
  serializable payloads, no prompts/secrets.
- **Failures**: tool crash → `FAILED`; provider failure → `FAILED`; exactly
  one terminal state per failure; failed event publisher never crashes the
  loop.
- **Deadlock regression**: verification for the approval gate is
  thread-based; the re-entrant-lock deadlock caught in Phase 5A is covered
  by the threaded decision tests.
- **Integration**: `greatsage agent health` (offline, exit 0, json), `agent
  run` with a scripted mock provider (one safe tool call, then final text),
  empty-prompt → exit 2, offline no-provider → exit 1, `--max-steps 1` →
  `limit_reached` exit 1.
## 6d. Phase 6 baseline (workspace / planning / task + CLI wiring)

- New suites: `tests/unit/workspace/` (models, scanner — depth/entry caps,
  timeout, entry-point detection), `tests/unit/planning/` (models incl.
  1..n sequence validation, deterministic planner incl. unknown-tool
  allow-list), `tests/unit/task/` (models, SQLite repository, bounded
  executor — unknown-tool denial, protected-path denial, timeouts),
  `tests/integration/test_cli_workspace_task.py` (scan/info/health,
  plan-file run/list/get round-trip, denial paths),
  `tests/integration/test_task_integration.py`,
  `tests/integration/test_cli_planning.py` (`planning create/get/list/
  health` — JSON + text output, empty-goal → exit 2, unknown id → exit 1,
  workspace-context attach, invalid config → exit 2).
- Key behaviors proven end-to-end through the real CLI: plan steps execute
  only via the Phase 4 tool pipeline (approval/policy denials hold);
  `workspace scan` outside allowed roots → exit 2; disabled subsystems
  report healthy no-op + clean `unavailable` failures.
- Packaging regression note: `tests/` must be a regular package
  (`__init__.py` in every test directory). Without it, an unrelated
  third-party `tests` bundle in `site-packages` shadows the local
  namespace-package `tests/` dir (regular packages win over namespace
  portions) and all `from tests.…` imports fail at collection with
  `ModuleNotFoundError: No module named 'tests.unit'`.
- Version regression note: `greatsage --version` reports the source
  `greatsage.__version__` as authoritative — a stale or unrelated installed
  distribution reusing the `great-sage` dist name must not mislabel running
  code (covered by `tests/integration/test_cli.py::test_version`).
## 6e. Phase 5B–10 baseline (delegation, autonomy, voice, vision, HUD, hardening)

- **1071 tests passing** (13 Sept 2026: 1071 total), `ruff check
  greatsage tests` clean, `mypy greatsage` clean (130 source files).
- New suites since §6d: `tests/unit/delegation/` (models, manager, service),
  `tests/unit/planning/test_graph.py` + `test_verify.py` (Phase 7 DAG +
  verification gate), `tests/unit/voice/` (6 files, stub backends),
  `tests/unit/vision/` (7 files, stub backend, no OCR),
  `tests/unit/interface/test_hud.py` (Phase 10 read-only dashboard),
  `tests/unit/tools/test_security_hardening.py` (Phase 9 redaction + policy
  tightening), `tests/unit/chat/test_chat.py` (21 tests: text/voice/exit/
  history/errors), plus CLI integration `test_cli_voice.py`,
  `test_cli_vision.py`, `test_cli_hud.py`.
- Test layout has converged to `tests/unit/<module>/` +
  `tests/integration/test_cli*.py` (the `tests/providers|tools|security|
  memory|tasks|opencode` top-level layout in §2 remains aspirational).
- Voice/vision suites assert stub honesty: mock STT/TTS, `[stub-no-ocr]`
  grounding marker, metadata-only capture — no model downloads, no network.
## 6f. Roadmap baseline (scheduler, tools, telegram, embeddings, real voice)

- **1071 tests passing** (13 Sept 2026), `ruff` + `mypy` clean
  (130 source files).
- New: `tests/unit/scheduler/` (models, repo, tick executor),
  `tests/integration/test_cli_schedule.py`,
  `tests/unit/tools/test_network_tools.py` + `test_gui_tools.py`
  (live click/type never exercised; screenshot live on Windows only),
  `tests/unit/telegram/` + `test_cli_telegram.py` (faked Bot API),
  `tests/unit/memory/test_memory_embeddings.py` (fake vectors; one
  Ollama-live probe kept out of the suite),
  `tests/unit/storage/test_recovery.py`,
  `tests/unit/voice/test_real_backends.py` (Vosk/Piper guarded by
  availability skips — CI-safe).
- Hermeticity rule (learned the hard way): no test may depend on the
  operator-owned `config/sage.yaml` existing or not —
  `test_defaults_load_without_file` and `test_config_validate_defaults`
  pin `DEFAULT_CONFIG_PATH` at a nonexistent path.
