# Autonomy (Phase 7)

> Bounded planner + task-graph + verification on top of the Phase 6
> workspace/planning/task subsystems and the Phase 5A agent loop.
> No unbounded autonomy: every multi-step run is verified, approved by a
> human where risk requires it, bounded by hard ceilings, and audited.

## 1. What Phase 7 Adds over Phase 6

| Capability | Phase 6 | Phase 7 |
|---|---|---|
| Plans | linear step list (sequence 1..n) | + optional `depends_on` task-graph (DAG) |
| Execution order | sequence order | topological order (linear plans unchanged) |
| Pre-run check | `Plan.validate()` (structure) | + `verify_plan()` (tools, risks, deps, cycles) |
| Human gate | none (ToolService approvals only) | `planning approve` (draft→ready) + `task run --approve` recorded |
| Audit | `PlanCreated/Failed`, `Task*`, `TaskStep*` | + `PlanVerified`, `PlanApproved`, `approved` flag on `TaskStarted` |
| CLI | `workspace`, `task` | + `planning` group, `task run --verify-only/--approve` |

The agent loop (`greatsage/agent/`) is unchanged: it still proposes one tool
call per step through `ToolService.execute`. Task-graph execution is the
multi-step sibling — deterministic plans through the same security
pipeline, never a bypass.

## 2. State Machines

### Plan lifecycle

```text
DRAFT --approve--> READY --task run--> RUNNING --all steps ok--> COMPLETED
  |                    |                     |
  | (verify fails)     | (step fails/        | (cancel/timeout)
  v                    v                     v
FAILED               FAILED          CANCELLED / TIMED_OUT
```

- `DRAFT`: created by `planning create` (or planner API). Not an
  endorsement — just a proposal.
- `READY`: a human ran `planning approve`, which first runs full
  verification and refuses to approve on any error. Only `DRAFT` plans
  can be approved; approval is never automatic and never silent
  (`PlanApproved` event).
- Terminal states (`COMPLETED/FAILED/CANCELLED/TIMED_OUT`) live on the
  **task**, not the plan: plans are reusable proposals, tasks are runs.

### Task lifecycle (unchanged from Phase 6)

`PENDING → RUNNING → COMPLETED | FAILED | CANCELLED | TIMED_OUT`,
with `PAUSED` for cooperative pause/resume. Resume skips already
completed steps by **step id** (correct under topo ordering).

## 3. Task-Graph Semantics (`greatsage/planning/graph.py`)

- `Step.depends_on: tuple[str, ...]` (default empty = sequential).
- `topological_order()`: Kahn's algorithm, stable — ready steps run in
  lowest-`sequence` order, so linear plans execute exactly as before.
- Fail-closed validation: unknown dep, self-dep, or cycle raises
  `PlanningValidationError` (at `Plan.validate()`, at `verify_plan()`,
  and at executor start). No partial ordering of an invalid graph.
- `levels()`: depth per step (0 = no deps) for reporting parallelism
  potential. Execution itself stays strictly sequential — one tool call
  at a time through `ToolService.execute`.

## 4. Verification (`greatsage/planning/verify.py`)

`verify_plan()` is pure (no I/O, no tool execution). It checks:

1. goal non-empty; ≥1 step; step count within limit;
2. sequences 1..n contiguous; unique step ids;
3. every `tool_id` in the known registry set (fail-closed);
4. `risk_estimate` in `{low, medium, high, critical}`;
5. `depends_on` references valid, no self-deps, no cycles;
6. flags `requires_approval: [step ids]` for `high`/`critical` risk;
7. warns on missing `acceptance_criteria`.

Report: `{plan_id, ok, errors[], warnings[], requires_approval[],
execution_order[], max_level}`. Entry points:

- `greatsage planning verify <plan_id>` (stored plans),
- `greatsage task run PLAN --verify-only` (files, ids, or dicts — no execution),
- `TaskService.verify_plan(plan)` (API, same reference types as `run()`).

## 5. Human-Gated Approvals

- No approval is ever assumed. `task run` records `human_approved: false`
  unless `--approve` is passed; the flag is stored in task metadata and
  the `TaskStarted` audit payload.
- `planning approve` is the plan-level gate: verify-must-pass +
  explicit command + `PlanApproved` event. Re-approving a non-draft plan
  is rejected.
- Tool-level `ASK` approvals still flow through the Phase 4
  `ToolService` pipeline (unchanged); a denial fails the step, fails the
  task, and is never auto-retried (see `docs/SECURITY_MODEL.md` §9,
  `docs/TOOLS.md` §6).
- An agent loop may retry a failed action; it may **not** retry a denied
  action.

## 6. Bounded Limits (all enforced, smallest wins)

| Limit | Default | Ceiling | Enforced at |
|---|---|---|---|
| plan steps (`planning.max_plan_steps`) | 25 | 50 | planner, verify, repository save |
| task steps (`task.max_steps`) | 25 | 50 | `TaskService.run`, executor |
| graph size | — | 50 (`MAX_PLAN_STEPS_CEILING`) | `topological_order` |
| per-step timeout | 30 s | 300 s | executor (tool layer bounds first) |
| total timeout | 600 s | 3600 s | executor |
| output | tool `max_output_bytes` 64 KiB | per tools config | `ToolService` |

Ceilings live in `greatsage/planning/limits.py` and
`greatsage/task/limits.py`; config validation rejects anything above them
at load time, and the executor re-checks during the run.

## 7. Audit Events (payloads carry ids/reasons only)

`PlanCreated → PlanVerified → PlanApproved → TaskStarted(approved=…)
→ TaskStepStarted/Completed/Failed… → TaskCompleted/Failed/Cancelled/TimedOut`.
Prompts, tool-argument values, and secrets are never published.

## 8. CLI Reference

```text
greatsage planning create --goal TEXT [--workspace PATH] [--json]
greatsage planning get <plan_id> [--json]
greatsage planning list [--json]
greatsage planning verify <plan_id> [--json]     # exit 1 when invalid
greatsage planning approve <plan_id> [--json]    # draft→ready, verify must pass
greatsage planning health [--json]
greatsage task run PLAN [--approve] [--verify-only] [--json]
```

Exit codes: `0` ok/approved/verified, `1` execution/verification failure,
`2` invalid input or configuration.

## 9. Testing Rules

- Offline determinism: `ToolService` fakes only; the security pipeline is
  never weakened in tests.
- Graph tests assert cycle/missing-dep/self-dep rejection and stable
  linear ordering.
- Approval tests assert fail-closed behavior: no flag → recorded
  `false`; non-draft → rejected; invalid plan → never approved.
- Verification: `pytest`, `ruff check .`, `mypy jarvis`, config
  validate, CLI smoke, tree clean, commit, stop.
