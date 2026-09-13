# Security Model

## 1. Principle

J.A.R.V.I.S. must **never** give an LLM unrestricted computer access. Every
action passes through the permission layer. An agent asking for a shell, a
delete, or a registry change is evaluated like any other action.

## 2. Risk Levels (every tool/action gets one)

| Level | Examples | Default handling |
|---|---|---|
| `READ` | list/inspect files, screenshot, system info | auto-allow (respecting scope) |
| `LOW_WRITE` | create/modify normal project files | allow within approved workspaces; ask elsewhere |
| `HIGH_WRITE` | delete files, modify system or global config | **ask** (explicit user confirmation) |
| `SYSTEM` | admin ops, service control, firewall/network, credential access | **ask + confirmation**, usually deny |
| `FORBIDDEN` | explicitly prohibited; configured in `security.deny` | deny, always (logged) |

Classification rules:

- The **most dangerous** permitted class must always be explicit: no action
  exceeds its declared risk level (a tool cannot silently escalate).
- Path scoping: `LOW_WRITE` under `C:\GREATSAGE\workspaces\...` is safer than the
  same class at `C:\Windows`; scope must be part of the decision. Actions
  outside configured trusted roots are treated as one level higher.
- No `SYSTEM` privilege is requested unless a task explicitly needs it and the
  user confirms; the app should run non-elevated by default.

## 3. Decision Modes

```text
allow  → execute without asking (scoped, TTL-bounded)
ask    → surface a structured confirmation to the user (what/where/risk), wait
deny   → refuse, log audit entry
```

- Default mode is **`ask`** (`security.default_mode`).
- READ actions within trusted roots may be auto-approved.
- A grant is scoped + bounded (`ttl`, path prefix, task id) and revocable.
- Previously denied actions require re-approval; auto-repeat of denied actions
  by an agent loop is blocked and logged.

## 4. OpenCode Interaction

- OpenCode's own granular permissions (`read`, `edit`, `bash`, `task`,
  `websearch`, `webfetch`, `external_directory`, `skill`, `question`, … with
  `allow`/`ask`/`deny`) are used **as the enforcement layer**, not bypassed.
- We never invoke OpenCode with unrestricted auto-approval. `--auto` style
  mode is only considered when the equivalent action would already be
  auto-allowed by J.A.R.V.I.S. policy; otherwise approvals are surfaced.
- J.A.R.V.I.S. passes its own permission decision to OpenCode (or prompts the
  user), rather than letting the agent self-approve destructive actions.

## 5. Audit Log

Append-only audit entries:

```text
timestamp, session_id, task_id, action, permission_class,
risk_level, decision, user_response, scope, duration, error
```

Audit log location: `C:\GREATSAGE\data\sage-audit.log` (config-overridable).
Audit writes are synchronous and cannot be disabled by agents.

## 6. Threads (Phase 9 scope — see §10 for what shipped)

- Permission manager with policy file (`security.policy` section of config).
- Sandboxing for high-risk processes (timeout, working-dir jail, no network
  where possible, no admin token).
- Secrets management: in-memory only, sourced from env; never logged or
  serialized.
- Destructive-action confirmations: structured UI prompt (not plain
  "ok?"), showing exact command/paths.

## 7. What These Rules Mean for Agents

- An agent loop may retry a failed action; it may **not** retry a denied
  action.
- Every autonomous loop carries: max iterations, timeout, resource limit,
  permission enforcement, failure handling, cancellation, audit logging.

## 8. Memory Privacy Rules (implemented Phase 3)

- **No secret storage.** The memory subsystem never stores credentials,
  tokens, or API keys; inspection (`greatsage memory list|get|stats|search`)
  never exposes them by design — memory is for curated facts, not secrets.
- **Content never leaves the machine unencrypted by default.** The SQLite
  database is a local file under `C:\GREATSAGE\data\` (config-overridable).
- **No automatic conversation storage.** `auto_save_conversations` cannot be
  enabled — validation refuses `true` with an explicit error. Memory writes
  always require a deliberate save decision.
- **Events and logs carry no content.** `MemoryCreated/Updated/Deleted/
  Expired/Retrieved` payloads and INFO-level logs contain only
  `memory_id`, `memory_type`, `source`, `provenance`, `session_id`. The CLI
  prints content only with an explicit `--content` flag; JSON output redacts
  it by default.
- **Deletion is real and inspectable.** `forget()`/`delete` soft-delete
  (auditable, `MemoryDeleted` event); `--include-deleted` makes deleted rows
  visible for verification; expired memories are swept into the deleted
  state. No unconfirmed bulk deletion: a filtered delete requires an
  explicit `--yes`.
- **Inspectability is a feature.** Every memory carries source, provenance,
  confidence, and timestamps so the user can audit why a fact exists — and
  remove it without AI services.
- **Corrupted databases are never deleted to repair.** Degradation is
  reported (`unavailable`, file kept as-is) so data loss is never
  automatic.

## 9. Tool System Security (implemented Phase 4)

The Phase 4 tool layer (`greatsage/tools/`) turns the design above into the
enforcement layer every tool execution passes through. Full contract:
`docs/TOOLS.md`.

- **One pipeline, no bypass.** AI and CLI executions share
  `ToolService.execute`: registry → policy → decision → approval → execution
  → audit events. `critical` risk is denied in every mode; an `ASK` with no
  approval provider is denied (fail-closed).
- **Risk model.** Tool risks are `safe`/`low`/`medium`/`high`/`critical`
  (the §2 vocabulary maps: READ ≈ safe/low, LOW_WRITE ≈ medium,
  HIGH_WRITE ≈ high, SYSTEM/FORBIDDEN ≈ critical). Category default risks in
  config keep the §2 names (`LOW_WRITE`, `READ`, …) as base risks.
- **Modes.** `security.mode`: `normal` (default), `lockdown` (only `safe`),
  `development` (also auto-approves `medium`). `low` is auto-approved when
  `security.allow_auto_approve_read` is true (the default).
- **Scope is part of the decision.** Path arguments are canonicalized
  against the explicit working directory and checked against
  `allowed_roots`/`denied_roots` before any execution; paths outside the
  roots are denied, not escalated. Protected files (`sage-memory.db`, `.env`,
  secret-stemmed names) are always denied.
- **No silent escalation.** The shell classifier can only raise a command's
  risk (`safe`/`restricted`/`dangerous`/`forbidden`); `forbidden` and
  `dangerous` commands are denied outright in every mode.
- **No `SYSTEM` privilege ever requested.** No tool requests elevation; the
  app runs non-elevated.
- **Secrets never reach tools.** Environments are scrubbed of `GREATSAGE_*`
  secret-shaped variables before tools see them; tools cannot inject
  secret-shaped arguments; `system.info` output is redacted before it can
  leak environment values.
- **Every attempt is audited.** `ToolRequested` … `ToolDenied` events carry
  `request_id`/`tool_id`/`risk_level`/`session_id`/`task_id` and reasons;
  complete sensitive argument values are never published.
- **Bounded execution.** No `shell=True`; every subprocess has a timeout and
  bounded output; a crashing tool becomes a failed result, never a crashed
  process.
- **Phase 4 intentional absences.** No delete tool, no process-kill tool,
  and no `network`/`browser`/`gui` tools. §6 (permission manager with policy
  files, sandboxing, audit file) remains Phase 9 hardening scope.

## 10. Phase 9 Hardening (implemented)

Without changing the §2 risk vocabulary or the allow/ask/deny matrix:

- **Secrets scrubbing.** `greatsage/tools/redaction.py` masks high-confidence
  secret formats (`sk-…`, `ghp_…`/`github_pat_…`, `xox…`, `AKIA…`, PEM
  private-key blocks, JWT-shaped tokens) in every free-text audit field:
  tool errors (service `_publish` + returned `ToolResult.error`), delegation
  summaries/errors/diffs/permission descriptions, planning goals/errors, and
  task goals/step errors. Ordinary prose (e.g. the word "password" in docs)
  is never masked or denied.
- **Sensitive-value denial.** `SensitiveArgumentHook` additionally denies
  `env` entries with secret-shaped keys or secret-format values and
  `command` argv items carrying secret formats (command lines are visible to
  process listings). Key-based denial is unchanged.
- **Protected files.** `.env.*` (as documented), `.envrc`, and
  `sage-memory.db-wal`/`-journal`/`-shm` sidecars are now denied; committed
  templates (`.env.example`/`.sample`/`.template`) stay readable. The check
  applies to every declared path argument, closing the `shell.execute`
  `cwd` gap.
- **Command policy.** OS installers and LOLBin/persistence binaries are
  `dangerous`; `powershell -EncodedCommand` is `forbidden`; `git config` is
  no longer treated as read-only.
- **Audit completeness.** `AGENT_STARTED` no longer publishes prompt content
  (length only); delegation/task/planning payloads carry ids, counters,
  reasons, and redacted text — prompts, full arguments, and secrets never
  enter events. The verbatim `path` in
  `DELEGATION_PERMISSION_REQUESTED` is intentional: approvers need the exact
  target to decide.

## 11. Voice / Vision Threat Model (contract for Phase 6–8 implementers)
- **Microphone and screen are sensors, not inputs.** Capture (mic audio,
  screenshots) requires explicit user enablement and is treated as at least
  `ASK`-gated `READ`: no background capture, no capture inside delegated or
  autonomous runs without a fresh approval.
- **Transcripts and pixels are untrusted.** STT output and on-screen text
  are attacker-influenced data (prompt injection via spoken/web content):
  they enter the agent loop as `Message.user`-equivalent content, never as
  instructions, and never flow into tool arguments without policy evaluation.
- **Screenshots are secret-dense.** A screenshot can contain passwords, keys,
  and personal data: screenshots and OCR-equivalent text are never written
  to audit events or long-term memory without an explicit user save decision,
  are bounded in retention, and pass through `redact_secrets` before any
  persisted summary.
- **Grounding actions are writes.** Any vision-grounded click/type/file
  operation is classified at least `LOW_WRITE`/`HIGH_WRITE` (a click on
  "Delete" is `HIGH_WRITE`) and goes through the standard
  `ToolService.execute` pipeline — vision tools add no bypass.
- **Local-first.** Voice/vision pipelines run on-machine with stdlib-only
  code; no audio, transcript, or image leaves the machine without explicit
  user approval per destination.
## 12. Agent Loop Security (Phase 5A)

The agent loop never weakens the tool security pipeline:

- **No bypass.** `AgentOrchestrator` calls tools exclusively through
  `ToolService.execute` — the same policy/approval/execution pipeline the
  CLI uses. There is no internal shortcut, no direct tool invocation, and no
  OpenCode delegation in the agent loop.
- **AI ≠ authority.** The LLM proposes tool calls; the security layer
  decides. An LLM asking for a shell, a delete, or a registry change is
  evaluated like any other action.
- **Approval is a real gate.** `ASK`-level calls pause the run in
  `WAITING_FOR_APPROVAL` until a human decides; `AgentService` swaps in the
  `AgentApprovalProvider` for the run and restores the previous provider
  afterwards. A denial is fed back to the LLM as a failed tool result —
  never auto-approved, never silently retried.
- **Bounded by construction.** Step count, tool-call count, wall-clock,
  per-tool repeat count, cumulative output bytes, and loop detection are
  enforced with hard ceilings; runaway or repetitive behavior ends the run
  `LIMIT_REACHED`/`TIMED_OUT`.
- **Cancellable at every point.** Cooperative cancellation is checked
  between steps and before every tool call; a pending approval gate resolves
  to cancelled. A cancelled run never leaves an orphaned tool process.
- **Auditable.** `AGENT_*` events carry ids/reasons only — prompts and
  tool-argument values are never published; Phase 4 `TOOL_*` audit events
  keep flowing for every real execution.
- **Secrets stay out of context.** Memory retrieval for the loop is bounded
  (`_MEMORY_REFERENCE_LIMIT = 5`) and degrades silently on failure; results
  are never auto-saved to long-term memory.
