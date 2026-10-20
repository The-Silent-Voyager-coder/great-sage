# Memory Architecture (Phase 3 implementation)

> Status: **implemented** (`greatsage/memory`, Phase 3). This documents the actual
> contract: schema, lifecycle, provenance, confidence, retention, retrieval,
> deletion, privacy, health, and the concurrency model. Embeddings/vector
> search and automatic conversation ingestion are explicitly **not** part of
> Phase 3 (see §9).

## 1. Categories

| Type | Content | Example | Persistence |
|---|---|---|---|
| **working** | current task/conversation context | "step 2 in progress: wiring CLI tests" | session RAM only (`WorkingMemoryStore`), TTL, no auto-promotion |
| **long_term** | stable, genuinely useful facts/preferences | "user's build machine is ASUS V16, 16 GB RAM" | SQLite, curated, persistent |
| **episodic** | records of Great Sage actions | "created project → edited X → maven failed → fixed dep → build ok" | SQLite, `record_episode()` |
| **semantic** | retrieved knowledge from documents/projects/notes | "API facts extracted from a README" | SQLite via `add_semantic()` — no ingestion pipeline yet |

## 2. Memory Entry Schema

```python
class MemoryType(StrEnum):
    WORKING = "working"          # session-scoped; RAM, not persisted
    LONG_TERM = "long_term"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"

class Provenance(StrEnum):
    USER_EXPLICIT = "user_explicit"
    SYSTEM_EVENT = "system_event"
    TOOL_RESULT = "tool_result"
    DOCUMENT = "document"
    AGENT_RESULT = "agent_result"
    IMPORT = "import"

@dataclass(frozen=True)
class Memory:
    id: str                     # "mem_<32 hex>" — UUID-based, never sequential
    memory_type: MemoryType
    content: str | dict | list  # plain text (FTS-indexed) or JSON data
    source: str                 # where it came from (session, file, tool, user)
    provenance: str             # canonical kinds above; any string accepted
    confidence: float           # 0.0..1.0
    created_at: datetime        # timezone-aware UTC
    updated_at: datetime
    expires_at: datetime | None
    metadata: Mapping           # structured extras
    session_id: str | None
    deleted_at: datetime | None # auditable soft delete
```

SQLite columns mirror the model: `id`, `memory_type`, `content` (JSON when
structured), `content_text` (FTS index), `source`, `provenance`, `confidence`,
`created_at`, `updated_at`, `expires_at`, `metadata_json`, `session_id`,
`deleted_at` — all timestamps stored as ISO-8601 UTC strings.

## 3. Rules (hard)

- **No automatic saving of conversations.** Every persistent write requires a
  `remember()` call with a deliberate save decision. `auto_save_conversations`
  exists in the config schema but validation **refuses** `true` — the flag
  cannot be enabled in Phase 3.
- **Never sequential ids.** Identifiers are `mem_<uuid>`; no enumerable order
  leaks through ids.
- **No unconfirmed bulk deletion.** `delete` removes one explicit id, or a
  filtered set **with an explicit `--yes`**; filters must be given — there is
  no "delete everything" shortcut.
- **Deletion is audit-safe.** `forget()`/`delete` soft-delete (`deleted_at`)
  and emit `MemoryDeleted`; rows are never hard-deleted by the API.
  Expired memories are swept into the deleted state, emitting `MemoryExpired`.
- **Structured content stays inspectable.** JSON content is stored as JSON
  and indexed via its textual representation; both forms render in CLI output.
- **Confidence is never fabricated.** `remember()` uses the caller's value or
  the configured default; there is no LLM-based confidence estimation in
  Phase 3.
- **Retrieval is deterministic and inspectable.** Same query + same data =
  same order. Every result carries a `match_reason` and its ranking score.
- **The user can always inspect and delete.** `greatsage memory list|get|stats|
  search|delete` work without any AI services; `--include-deleted` and
  `--include-expired` make even hidden rows visible for inspection.

## 4. Lifecycle

```text
create (remember / record_episode / add_semantic)
  → active (retrievable, listed, searchable)
  → expired (expires_at passed → swept to deleted, MemoryExpired)
  → deleted (forget / delete → deleted_at set, MemoryDeleted)

update: content / confidence / expires_at / metadata only.
        provenance, source, memory_type, session_id, created_at are immutable.
        updated_at always changes.
```

- `remember()` defaults: `memory_type=LONG_TERM`, `source=""`,
  `provenance="user_explicit"`, `confidence=memory.default_confidence`,
  no expiration. WORKING and EPISODIC entries default to
  `retention_days` expiration when no `expires_at` is given.
- Expiration is checked lazily: retrieval/list/search exclude expired rows by
  default; `expire()` sweeps them into the deleted state; explicit
  inspection flags (`--include-expired`, `--include-deleted`) reveal them.

## 5. Retrieval & Ranking

Filters (all deterministic, combinable): memory type, source, provenance,
`created_after`/`created_before`, `expires_before`, `minimum_confidence`,
session_id, plus `include_expired`/`include_deleted` and `limit`/`offset`.

**Ranking formula** (service-side, over the repository's full matching set
ordered `created_at DESC, id ASC`):

```text
score = 0.5 · relevance + 0.3 · confidence + 0.2 · recency
recency = 1 / (1 + age_days)
```

- In Phase 3 `relevance` is literal: `1.0` for a query match, `0.0` for a
  plain listing — future semantic phases replace it with embedding similarity
  without changing the formula or the API.
- Results are `RankedMemory(memory, score, match_reason)` with reason
  `"query match: <query>"` or `"listed"`; `total` counts the full match set
  before pagination.
- Full-text search: SQLite FTS5 (external-content table) when available,
  with a `LIKE ... ESCAPE '\'` fallback otherwise — both paths exclude
  expired/deleted rows by default.

## 6. Working Memory (session RAM)

`WorkingMemoryStore` owns per-session `WorkingMemory` buckets:

- `add(content, ttl_seconds=None, item_id=None) -> id` — default TTL
  `WORKING_DEFAULT_TTL_SECONDS`; `ttl_seconds ≤ 0` means no expiry.
- `get`/`remove`/`list` (active items, newest first) / `sweep` / `clear`.
- **Strict session isolation**: items are keyed by `session_id`; one session
  can never read or remove another session's items.
- Working memory **never** auto-promotes to long-term memory; promotion is a
  deliberate decision made by future phases through `remember()`.
- Working items are RAM-only and are not persisted to SQLite.

## 7. Storage & Concurrency

- SQLite at `memory.database_path` (default `C:/GREATSAGE/data/sage-memory.db`);
  parent directories created on first use; `enabled: false` creates nothing.
- Schema versioning: `schema_meta(key, value)` stores `schema_version`;
  migrations are ordered stdlib SQL statements executed in a transaction.
  A database whose schema version is **newer** than this build is refused —
  never touched. A **corrupted** database is never repaired by deleting it:
  the subsystem reports `unavailable` (detail names the file and error,
  "file kept as-is").
- WAL mode + `busy_timeout`; single connection guarded by `RLock`; writes are
  transactional (no partial rows). This is a deliberate small local
  concurrency strategy — no distributed locking, single-process writer.
- FTS5 external-content table with sync triggers; `fts_enabled` reported by
  health and stats.

## 8. Events & Observability

| Event | Payload (never content) |
|---|---|
| `MemoryCreated` | memory_id, memory_type, source, provenance, session_id |
| `MemoryUpdated` | memory_id, memory_type, source, provenance, session_id |
| `MemoryDeleted` | memory_id, memory_type, source, provenance, session_id |
| `MemoryExpired` | memory_id, memory_type, source, provenance, session_id |
| `MemoryRetrieved` | memory_id, memory_type, source, provenance, session_id |

INFO logs likewise carry only ids/types/sources. Content appears only on the
CLI with `--content` and never in events or default logs.

**Health** (`greatsage memory health`, runtime `memory` check): `accessible`,
`schema_valid`, `migrations_current`, `writable`, `fts_enabled`,
`schema_version`, `database_path`, `detail`. Status is HEALTHY when the
database is accessible and correct and a write probe succeeds; a failed
initialization reports `unavailable` with detail while the runtime and every
other subsystem keep running (failure isolation). `enabled: false` reports
HEALTHY as a deliberate no-op.

## 9. Out of Scope (later phases)

- Embeddings / vector search / semantic similarity (replaces the literal
  relevance term, formula unchanged).
- Automatic conversation-to-memory extraction and LLM-based triage.
- Semantic-memory ingestion pipelines over documents/projects.
- Cross-host synchronization, distributed storage, multi-process writers.