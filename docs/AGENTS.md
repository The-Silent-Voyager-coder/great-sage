# Agents

> Phase 5A — the bounded agent tool loop. Contract for the agent subsystem
> (`greatsage/agent/`) and the operating rules agents follow.

## 1. Authority Hierarchy

**AI ≠ authority; Tool Security = authority; Agent Orchestrator = control
flow.**

- The LLM (via a provider-neutral `AIProvider`) *proposes* tool calls. It
  never reaches a tool, a file, a process, or the shell directly.
- `ToolService.execute` is the **only** path from the agent to a tool: the
  full Phase 4 security pipeline (registry validation → SecurityPolicy
  ALLOW/ASK/DENY → approval provider → bounded execution → `TOOL_*` audit
  events) always runs. There is no internal bypass for agent runs.
- The `AgentOrchestrator` owns the loop (generate → propose → execute →
  observe → repeat, bounded); the `AgentService` owns gating, limits,
  approval swap, memory retrieval, cancellation, and health.
- Nothing in the loop is OpenCode-specific; OpenCode delegation lives in
  the Phase 5B delegation layer (`greatsage/delegation/`) and is out of scope
  for the loop itself — the orchestrator never calls it directly.

## 2. State Machine

Valid states: `created`, `running`, `executing_tool`,
`waiting_for_approval`, `completed`, `failed`, `cancelled`, `timed_out`,
`limit_reached`. All transitions are validated — invalid ones raise
`AgentStateError`.

Transitions:

```text
created → running                       (run starts)
running ⇄ executing_tool                (each step/tool call)
executing_tool → waiting_for_approval   (ASK-level call, gate open)
waiting_for_approval → executing_tool   (approved / denied → resolved)
running → completed | failed | cancelled | timed_out | limit_reached
```

Exactly **one** terminal state per failure: a tool crash or provider failure
ends `failed`; wall-clock exhaustion ends `timed_out`; limit exhaustion ends
`limit_reached` with the exact reason; cancellation ends `cancelled`.

## 3. Loop Semantics (one tool call per step)

1. `AGENT_STARTED`; validation and limit checks; loop while `running`.
2. `AGENT_STEP_STARTED` → generate (provider-neutral `AIRequest`), subject
   to `max_steps`/wall-clock.
3. `AGENT_TOOL_CALL_REQUESTED` — strictly sequential: one tool call per
   step; **no parallel calls**. Loop-detection check runs before the call.
4. `ToolService.execute` (always) → `AGENT_TOOL_CALL_COMPLETED` carrying
   `success: true|false` (approval denials and tool failures arrive as failed
   `ToolCallResult`s in the same event — there is no separate failed event);
   the result becomes the tool message for the next step.
5. `AGENT_STEP_COMPLETED`; repeat until the provider stops requesting tools
   → `AGENT_COMPLETED`.

Tool calls carry structured arguments only (`id`, `tool_id`, `arguments`,
`sequence`) — never an opaque shell string.

The run's system note tells the model to answer directly when no tool is
needed, avoid homeassistant/network/gui tools unless explicitly asked, and
explain tool failures in plain language instead of printing raw error JSON.
Adherence is model-dependent; the tool security pipeline remains authoritative.

## 4. Hard Limits (config + ceilings)

| Limit | Default | Ceiling |
|---|---|---|
| `max_steps` | 12 | 25 |
| `max_tool_calls` | 8 | 50 |
| `max_wall_time_seconds` | 300 | 1800 |
| `max_single_tool_calls` | 3 | 10 |
| `max_total_tool_output_bytes` | 2 MiB (2097152) | 16 MiB (16777216) |
| `loop_detection_threshold` | 3 | 25 |

- The smaller applicable limit always wins (config vs. ceiling vs.
  run-time override). Enforced at run start, not loosely during the run.
- `loop_detection_threshold`: consecutive identical tool-call signatures
  (same tool + arguments) end the run `limit_reached` with reason
  `loop_detection`. Semantic loop detection is out of scope.

## 5. Approval

- `AgentService` swaps in an `AgentApprovalProvider` (thread-safe) before a
  run and restores the previous provider afterwards.
- An `ASK`-level call opens the gate and pauses the state machine in
  `waiting_for_approval` until a human decides (`approve` / `reject` /
  timeout / cancellation).
- Binary vocabulary only: `APPROVED` / `DENIED`. A denial is **never**
  auto-approved and never silently retried — it is fed back to the LLM as a
  failed tool result.
- No approval provider + `ASK` decision → denied (fail-closed).
- The CLI `--interactive` path uses `ApprovalBroker`
  (`greatsage/agent/approval_broker.py`): the agent runs in a worker thread,
  the CLI presents each pending request (tool, risk, reason, redacted argument
  preview), and `y`/`yes` approves while anything else denies. EOF and the
  bounded wait deny; Ctrl+C rejects outstanding requests and cancels the run.
  The broker exposes `pending()`/`approve()`/`reject()` for a future UI without
  changing the tool pipeline or granting it any new authority.
- The local control plane (`greatsage serve`) uses that same broker for its
  approve/reject buttons and starts agent runs through `AgentService.run`.
  Plan/task runs go through `TaskService.run`/`resume` with an explicit
  `approved=True` human gate; the control plane adds no direct tool path and
  no approval authority of its own.

## 6. Cancellation and Timeouts

- Cooperative `CancellationToken`: `cancel(task_id)` flips it; the
  orchestrator checks it between steps, after each provider generation, and
  before every tool call; a pending approval gate resolves to cancelled.
  Ollama requests have no server-side cancel handle, so an in-flight
  generation ends the run `cancelled` once the provider returns.
- Wall-clock timeout (`max_wall_time_seconds`) ends `timed_out`.
- Per-tool execution timeouts are enforced by the tool layer itself
  (`tools.execution_timeout_seconds`).
- A cancelled/timed-out run never leaves an orphaned tool process or a
  non-terminal AgentResult.

## 7. Provider Gating

`AgentService.run` requires, in order, and raises a typed error otherwise
(no silent fallback):

- config `agent.enabled` → else `AgentUnavailableError` ("disabled")
- service started → else `AgentUnavailableError` ("not started")
- dependencies wired (intelligence + tools) → else `AgentUnavailableError`
- an owned provider (explicit, else the configured default) that is
  registered → else `ProviderUnavailableError` ("not registered")
- provider state `READY` → else `ProviderUnavailableError` ("unavailable")
- provider capability `TOOL_CALLING` → else `ProviderCapabilityError`

## 8. Memory in the Loop

- Retrieval is explicit and bounded: at most `_MEMORY_REFERENCE_LIMIT = 5`
  references (`memory_references` on `AgentContext`), scoped to the run's
  `session_id` when one is given. The orchestrator injects them into the
  system prompt, clearly delimited as untrusted context (data, never
  instructions).
- Retrieval failure degrades silently (the run continues without context).
- The loop **never** auto-saves to long-term memory (Phase 3 rule).

## 9. Events

`AGENT_*` events carry ids/reasons only — prompts, tool-argument values,
model output, and secrets are never published. `AGENT_COMPLETED` carries
`content_length`; the final text stays in the `AgentResult`/control state.
Phase 4 `TOOL_*` events keep flowing for every real execution. Exact order for one tool call:

```text
AGENT_STARTED → AGENT_STEP_STARTED → AGENT_TOOL_CALL_REQUESTED →
AGENT_TOOL_CALL_COMPLETED → AGENT_STEP_COMPLETED → AGENT_STEP_STARTED →
AGENT_STEP_COMPLETED (final) → AGENT_COMPLETED
```

## 10. CLI

```text
greatsage agent health [--config PATH] [--json]
greatsage agent run --prompt TEXT [--session-id SID] [--provider NAME]
                  [--model NAME] [--max-steps N] [--interactive]
                  [--json] [--config PATH]
greatsage agent history [--limit N] [--config PATH] [--json]
```

Terminal runs are recorded in `core.data_dir/sage-agent-runs.db` as metadata
only (state, counts, provider/model, timings, redacted error/reason, prompt and
output lengths). Prompts and model output are never stored. History failure
never fails a run; `agent health` reports the history store state.

`--interactive` prompts on stdin for every `ASK`-level tool call. Without it,
the bounded agent approval window expires and denies — approval is never
assumed.

Exit codes: `0` completed/healthy; `1` failure (unavailable provider,
non-completed state, runtime failure); `2` invalid prompt/configuration.

## 11. Testing Rules

- Offline determinism: providers are faked (scripted `MockProvider` /
  `FakeProvider`); the tool security pipeline is never weakened in tests.
- No-bypass proof: agent tests only ever reach tools through the
  `ToolService`-shaped fake, never direct tool invocation.
- Event ordering and serialization are asserted for the exact single-call
  sequence above; a failed event publisher never crashes the loop.
- Approval tests are threaded (decision arriving while the waiter is in the
  decision window) to catch re-entrant-lock deadlocks — the `approval.py`
  snapshot-then-consume fix is covered by them.
- Verification: `pytest`, `ruff check .`, `mypy greatsage`, config validate,
  CLI smoke, tree clean, commit, stop.