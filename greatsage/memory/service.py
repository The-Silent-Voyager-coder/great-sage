"""Memory service facade: owns memory business rules.

Responsibilities (Phase 3 §18): validation, normalization, creation,
retrieval, updating, deletion, expiration, ranking, and event emission.
The SQLite repository stays a persistence mechanism behind the
MemoryRepository abstraction.

Privacy rules (docs/SECURITY_MODEL.md + §25): events and logs never carry
memory content; secrets must not be stored as provenance metadata; memory is
never auto-saved from conversations.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from greatsage.configuration.model import SageConfig
from greatsage.core.health import HealthRegistry, HealthStatus
from greatsage.events.models import (
    MEMORY_CREATED,
    MEMORY_DELETED,
    MEMORY_EXPIRED,
    MEMORY_RETRIEVED,
    MEMORY_UPDATED,
    Event,
)
from greatsage.exceptions import (
    MemoryNotFoundError,
    MemoryUnavailableError,
    MemoryValidationError,
)
from greatsage.memory.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    OllamaEmbeddingProvider,
    cosine_similarity,
)
from greatsage.memory.models import (
    Memory,
    MemoryContent,
    MemoryFilter,
    MemoryRetrieval,
    MemoryType,
    Provenance,
    RankedMemory,
    content_text,
    new_memory_id,
    utcnow,
)
from greatsage.memory.repository import MemoryRepository
from greatsage.memory.sqlite_repository import SqliteMemoryRepository
from greatsage.memory.working_memory import WorkingMemory, WorkingMemoryStore

log = logging.getLogger("greatsage.memory.service")

# Deterministic ranking formula (documented in docs/MEMORY.md §ranking):
#   score = 0.50 * relevance + 0.30 * confidence + 0.20 * recency
#   relevance = 1.0 for matched entries (0 otherwise — non-matches are excluded)
#   recency = 1 / (1 + age_seconds / 86400)
RANK_RELEVANCE = 0.5
RANK_CONFIDENCE = 0.3
RANK_RECENCY = 0.2
DAY_SECONDS = 86400.0

_MISSING: Any = object()


def _matches_filters(memory: Memory, filters: MemoryFilter, now: datetime) -> bool:
    """Mirror the repository's filter semantics for the semantic path.

    The vector index has no SQL filters, so the same predicates are applied
    in Python; anything that differed here would make `--semantic` return
    rows the text search would never show (or hide rows it would).
    """
    if filters.memory_type is not None and memory.memory_type != filters.memory_type:
        return False
    if filters.source is not None and memory.source != filters.source:
        return False
    if filters.provenance is not None and memory.provenance != filters.provenance:
        return False
    if filters.created_after is not None and memory.created_at <= filters.created_after:
        return False
    if filters.created_before is not None and memory.created_at >= filters.created_before:
        return False
    if filters.expires_before is not None and (
        memory.expires_at is None or memory.expires_at >= filters.expires_before
    ):
        return False
    if filters.minimum_confidence is not None and float(memory.confidence) < float(
        filters.minimum_confidence
    ):
        return False
    if not filters.include_deleted and memory.deleted_at is not None:
        return False
    if (
        not filters.include_expired
        and memory.expires_at is not None
        and memory.expires_at <= now
    ):
        return False
    if filters.session_id is not None and not (
        memory.session_id is None or memory.session_id == filters.session_id
    ):
        return False
    return True


class MemoryService:
    """Facade over the repository and working memory (owned by the runtime)."""

    def __init__(
        self,
        repository: MemoryRepository | None = None,
        working_store: WorkingMemoryStore | None = None,
    ) -> None:
        self._repository = repository
        self._working = working_store or WorkingMemoryStore()
        self.publisher: Any = None  # callable(event) -> None, wired by runtime
        self.availability: str = "unavailable"  # disabled | healthy | unavailable
        self.detail: str = "memory service not started"
        self._config: SageConfig | None = None

    # --- lifecycle -----------------------------------------------------

    def start(self, config: SageConfig | None = None) -> None:
        """Initialize the repository from config; failures never raise.

        A missing/corrupt/unwritable database leaves the service in
        'unavailable' state — the runtime and other components keep working
        (Phase 3 §37 failure isolation).
        """
        self._config = config
        if config is None:
            self.availability = "unavailable"
            self.detail = "no configuration provided"
            return
        memory = config.memory
        if not memory.enabled:
            self.availability = "disabled"
            self.detail = "memory subsystem disabled by configuration"
            log.info("memory service disabled by configuration", extra={"component": "memory"})
            return
        try:
            repository = self._repository or SqliteMemoryRepository(memory.database_path)
            repository.initialize()
            self._repository = repository
        except Exception as exc:
            self.availability = "unavailable"
            self.detail = str(exc)[:300]
            log.error(
                "memory service failed to initialize: %s",
                exc,
                exc_info=True,
                extra={"component": "memory"},
            )
            return
        self.availability = "healthy"
        self.detail = "memory database ready"
        log.info(
            "memory service started",
            extra={
                "component": "memory",
                "database_path": str(memory.database_path),
                "fts_enabled": repository.fts_enabled,
            },
        )

    def shutdown(self) -> None:
        if self._repository is not None:
            try:
                self._repository.close()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("memory repository close failed: %s", exc)
            self._repository = None
        self.availability = "disabled"
        self.detail = "memory service stopped"
        log.info("memory service stopped", extra={"component": "memory"})

    # --- public API ----------------------------------------------------

    def remember(
        self,
        content: MemoryContent,
        *,
        memory_type: MemoryType = MemoryType.LONG_TERM,
        source: str = "user",
        provenance: str = Provenance.USER_EXPLICIT.value,
        confidence: float | None = None,
        expires_at: Any = None,
        metadata: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Memory:
        """Create a memory (defaults to LONG_TERM; never called automatically).

        WORKING and EPISODIC memories default to an expiration of
        `memory.retention_days` when no explicit expiry is given; LONG_TERM
        and SEMANTIC memories are persistent unless told otherwise.
        """
        conf = self._effective_confidence(confidence)
        expiry = self._effective_expiry(memory_type, expires_at)
        memory = Memory(
            id=new_memory_id(),
            memory_type=memory_type,
            content=content,
            source=source,
            provenance=provenance,
            confidence=conf,
            created_at=utcnow(),
            updated_at=utcnow(),
            expires_at=expiry,
            metadata=dict(metadata or {}),
            session_id=session_id,
        )
        memory.validate()
        repository = self._require_available()
        repository.create(memory)
        self._maybe_embed(memory)
        self._publish(
            MEMORY_CREATED,
            {
                "memory_id": memory.id,
                "memory_type": memory.memory_type.value,
                "source": memory.source,
                "provenance": memory.provenance,
                "session_id": session_id,
            },
        )
        log.info(
            "memory created",
            extra={
                "component": "memory",
                "memory_id": memory.id,
                "memory_type": memory.memory_type.value,
                "source": memory.source,
                "provenance": memory.provenance,
            },
        )
        return memory

    def record_episode(
        self,
        content: MemoryContent,
        *,
        action: str | None = None,
        source: str = "system",
        provenance: str = Provenance.SYSTEM_EVENT.value,
        confidence: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Memory:
        """Record an episode (Great Sage action/event) — EPISODIC memory."""
        extra = dict(metadata or {})
        if action:
            extra["action"] = action
        return self.remember(
            content,
            memory_type=MemoryType.EPISODIC,
            source=source,
            provenance=provenance,
            confidence=confidence,
            metadata=extra,
            session_id=session_id,
        )

    def add_semantic(
        self,
        content: MemoryContent,
        *,
        source: str = "document",
        provenance: str = Provenance.DOCUMENT.value,
        confidence: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Memory:
        """Add a semantic memory (structured knowledge foundation, §31)."""
        return self.remember(
            content,
            memory_type=MemoryType.SEMANTIC,
            source=source,
            provenance=provenance,
            confidence=confidence,
            metadata=metadata,
        )

    def retrieve(
        self,
        query: str | None = None,
        *,
        memory_type: MemoryType | None = None,
        source: str | None = None,
        provenance: str | None = None,
        created_after: Any = None,
        created_before: Any = None,
        expires_before: Any = None,
        minimum_confidence: float | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
        semantic: bool = False,
    ) -> MemoryRetrieval:
        """Deterministic retrieval with filtering + documented ranking (§19-22).

        Defaults exclude expired and deleted memories; expired/deleted are
        explicitly inspectable via flags. Results keep IDs, provenance, and a
        match reason.

        `semantic=True` ranks by cosine similarity against stored embedding
        vectors instead of text matching. It requires `memory.embeddings_enabled`
        and a reachable backend — both failures raise instead of silently
        falling back to text search.
        """
        if query is not None and not str(query).strip():
            raise MemoryValidationError("search query must be a non-empty string")
        filters = MemoryFilter(
            memory_type=memory_type,
            source=source,
            provenance=provenance,
            created_after=created_after,
            created_before=created_before,
            expires_before=expires_before,
            minimum_confidence=minimum_confidence,
            session_id=session_id,
            include_expired=include_expired,
            include_deleted=include_deleted,
        )
        filters.validate()
        if limit is not None and limit < 0:
            raise MemoryValidationError("limit must be >= 0")
        if offset < 0:
            raise MemoryValidationError("offset must be >= 0")

        repository = self._require_available()
        if semantic:
            return self._retrieve_semantic(
                repository,
                str(query).strip() if query else "",
                filters,
                limit=limit,
                offset=offset,
            )
        if query is not None:
            matches = repository.search(query, filters)
            reason = f"query match: {query}"
        else:
            matches = repository.list(filters)
            reason = "listed"
        ranked = self._rank(matches, reason)
        total = len(ranked)
        if limit is not None:
            ranked = ranked[offset : offset + limit]
        else:
            ranked = ranked[offset:]
        self._publish(
            MEMORY_RETRIEVED,
            {
                "query": query,
                "count": len(ranked),
                "total": total,
                "ids": [item.memory.id for item in ranked][:20],
                "session_id": session_id,
            },
        )
        return MemoryRetrieval(items=ranked, total=total)

    def get(
        self,
        memory_id: str,
        *,
        include_expired: bool = False,
        include_deleted: bool = False,
    ) -> Memory | None:
        repository = self._require_available()
        return repository.get(
            memory_id,
            include_expired=include_expired,
            include_deleted=include_deleted,
        )

    def update(
        self,
        memory_id: str,
        *,
        content: Any = _MISSING,
        confidence: float | None = _MISSING,
        expires_at: Any = _MISSING,
        metadata: Mapping[str, Any] | None = _MISSING,
    ) -> Memory:
        """Update a memory, preserving provenance/source/type/session (§23).

        updated_at always changes; original source metadata is never erased.
        """
        repository = self._require_available()
        memory = repository.get(memory_id)
        if memory is None or memory.deleted_at is not None:
            raise MemoryNotFoundError(f"memory not found: {memory_id}")
        new_confidence = memory.confidence
        if confidence is not _MISSING:
            if confidence is None:
                raise MemoryValidationError("confidence must be between 0.0 and 1.0")
            new_confidence = confidence
        updated = replace(
            memory,
            content=memory.content if content is _MISSING else content,
            confidence=new_confidence,
            expires_at=memory.expires_at if expires_at is _MISSING else expires_at,
            metadata=memory.metadata if metadata is _MISSING else dict(metadata or {}),
            updated_at=utcnow(),
        )
        updated.validate()
        repository.update(updated)
        if content is not _MISSING:
            try:
                repository.delete_embedding(updated.id)
            except NotImplementedError:
                pass
            self._maybe_embed(updated)
        self._publish(
            MEMORY_UPDATED,
            {
                "memory_id": updated.id,
                "memory_type": updated.memory_type.value,
                "source": updated.source,
                "provenance": updated.provenance,
                "session_id": updated.session_id,
            },
        )
        return updated

    def forget(self, memory_id: str) -> None:
        """Soft-delete a memory (auditable, MemoryDeleted event)."""
        repository = self._require_available()
        memory = repository.get(memory_id, include_deleted=True)
        if memory is None:
            raise MemoryNotFoundError(f"memory not found: {memory_id}")
        if not repository.delete(memory_id, utcnow()):
            raise MemoryNotFoundError(f"memory not found: {memory_id}")
        try:
            repository.delete_embedding(memory_id)  # vectors die with the memory
        except NotImplementedError:
            pass
        self._publish(
            MEMORY_DELETED,
            {
                "memory_id": memory.id,
                "memory_type": memory.memory_type.value,
                "source": memory.source,
                "provenance": memory.provenance,
                "session_id": memory.session_id,
            },
        )
        log.info(
            "memory deleted",
            extra={
                "component": "memory",
                "memory_id": memory.id,
                "memory_type": memory.memory_type.value,
            },
        )

    def expire(self) -> int:
        """Sweep expired memories into deleted state; emit MemoryExpired."""
        repository = self._require_available()
        ids = repository.expire(utcnow())
        for memory_id in ids:
            memory = repository.get(memory_id, include_expired=True, include_deleted=True)
            if memory is None:  # pragma: no cover - defensive
                continue
            self._publish(
                MEMORY_EXPIRED,
                {
                    "memory_id": memory.id,
                    "memory_type": memory.memory_type.value,
                    "source": memory.source,
                    "provenance": memory.provenance,
                },
            )
        if ids:
            log.info(
                "memory expiration sweep",
                extra={"component": "memory", "count": len(ids)},
            )
        return len(ids)

    def working(self, session_id: str) -> WorkingMemory:
        """Access a session's RAM working memory (isolated per session)."""
        return self._working.session(session_id)

    def drop_working_session(self, session_id: str) -> bool:
        return self._working.drop(session_id)

    # --- inspection ------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        repository = self._require_available()
        return repository.stats()

    def health(self) -> dict[str, Any]:
        if self.availability == "healthy" and self._repository is not None:
            repo = self._repository.health().to_dict()
        else:
            repo = {}
        embeddings: dict[str, Any] = {"enabled": False}
        if self._config is not None and self._config.memory.embeddings_enabled:
            embeddings = {"enabled": True, "model": self._config.memory.embedding_model}
            try:
                probe = OllamaEmbeddingProvider(
                    self._config.memory.embedding_base_url,
                    self._config.memory.embedding_model or DEFAULT_EMBEDDING_MODEL,
                    timeout_seconds=5.0,
                ).health()
                embeddings.update(probe)
            except Exception as exc:  # pragma: no cover - defensive
                embeddings.update({"available": False, "detail": str(exc)[:200]})
        return {
            "available": self.availability == "healthy",
            "status": self.availability,
            "detail": self.detail,
            "embeddings": embeddings,
            **repo,
        }

    def register_health_check(self, health_registry: HealthRegistry) -> None:
        def checker() -> HealthStatus:
            if self.availability == "disabled":
                return HealthStatus.HEALTHY
            if self.availability == "healthy":
                return HealthStatus.HEALTHY
            if self.availability == "unavailable":
                return HealthStatus.UNHEALTHY
            return HealthStatus.DEGRADED  # pragma: no cover - future states

        health_registry.register("memory", checker, "memory subsystem (SQLite)")

    # --- internals -------------------------------------------------------

    def _require_available(self) -> MemoryRepository:
        if self.availability != "healthy" or self._repository is None:
            raise MemoryUnavailableError(self.detail or "memory subsystem is not available")
        return self._repository

    def _effective_confidence(self, confidence: float | None) -> float:
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise MemoryValidationError(f"confidence must be a number, got {confidence!r}")
            if not 0.0 <= confidence <= 1.0:
                raise MemoryValidationError(
                    f"confidence must be between 0.0 and 1.0, got {confidence}"
                )
            return float(confidence)
        if self._config is not None:
            return float(self._config.memory.default_confidence)
        return 0.8

    def _effective_expiry(self, memory_type: MemoryType, expires_at: Any) -> Any:
        if expires_at is not None:
            if not hasattr(expires_at, "tzinfo") or expires_at.tzinfo is None:
                raise MemoryValidationError("expires_at must be a timezone-aware datetime")
            return expires_at
        if memory_type in (MemoryType.WORKING, MemoryType.EPISODIC):
            days = (
                self._config.memory.retention_days
                if self._config is not None
                else 365
            )
            if days > 0:
                return utcnow() + timedelta(days=days)
        return None

    def reindex_embeddings(self, limit: int = 500) -> dict[str, Any]:
        """Backfill vectors for live memories missing them (explicit action).

        Returns {model, embedded, failed, skipped}. Backend failures are
        counted, never raised; a disabled subsystem raises
        MemoryValidationError instead of pretending to work.
        """
        provider = self._embedding_provider(required=True)
        assert provider is not None
        repository = self._require_available()
        model = provider.model
        try:
            backlog = repository.missing_embeddings(model, limit=max(0, limit))
        except NotImplementedError as exc:
            raise MemoryUnavailableError("embeddings unsupported by this repository") from exc
        embedded = failed = 0
        for memory_id in backlog:
            memory = repository.get(memory_id)
            if memory is None:
                continue
            try:
                vectors = provider.embed([content_text(memory.content)])
                repository.save_embedding(memory_id, model, vectors[0])
                embedded += 1
            except MemoryUnavailableError:
                failed += 1
            except NotImplementedError:
                raise MemoryUnavailableError("embeddings unsupported by this repository")
        return {"model": model, "embedded": embedded, "failed": failed, "skipped": 0}

    def _embedding_provider(self, *, required: bool) -> OllamaEmbeddingProvider | None:
        """Build the embedding backend when enabled; None (or raise) otherwise."""
        if self._config is None or not self._config.memory.embeddings_enabled:
            if required:
                raise MemoryValidationError(
                    "semantic recall needs memory.embeddings_enabled=true"
                )
            return None
        return OllamaEmbeddingProvider(
            self._config.memory.embedding_base_url,
            self._config.memory.embedding_model or DEFAULT_EMBEDDING_MODEL,
        )

    def _maybe_embed(self, memory: Memory) -> None:
        """Best-effort vector write after a save; failures degrade silently."""
        try:
            provider = self._embedding_provider(required=False)
            if provider is None:
                return
            repository = self._require_available()
            vectors = provider.embed([content_text(memory.content)])
            repository.save_embedding(memory.id, provider.model, vectors[0])
        except MemoryUnavailableError as exc:
            log.warning(
                "embedding write skipped for %s: %s",
                memory.id,
                exc,
                extra={"component": "memory"},
            )
        except NotImplementedError:
            log.debug("embeddings unsupported by repository", extra={"component": "memory"})

    def _retrieve_semantic(
        self,
        repository: MemoryRepository,
        query: str,
        filters: MemoryFilter,
        *,
        limit: int | None,
        offset: int,
    ) -> MemoryRetrieval:
        if not query:
            raise MemoryValidationError("semantic search needs a non-empty query")
        provider = self._embedding_provider(required=True)
        assert provider is not None
        vectors = provider.embed([query])
        try:
            stored = repository.list_embeddings(provider.model)
        except NotImplementedError as exc:
            raise MemoryUnavailableError("embeddings unsupported by this repository") from exc
        scored: list[tuple[float, str]] = sorted(
            (
                (cosine_similarity(vectors[0], vector), memory_id)
                for memory_id, vector in stored.items()
            ),
            reverse=True,
        )[:5000]
        now = utcnow()
        ranked: list[RankedMemory] = []
        for similarity, memory_id in scored:
            if similarity <= 0.0:
                break
            memory = repository.get(
                memory_id,
                include_expired=filters.include_expired,
                include_deleted=filters.include_deleted,
            )
            if memory is None:
                continue
            if not _matches_filters(memory, filters, now):
                continue
            age_seconds = max((now - memory.created_at).total_seconds(), 0.0)
            recency = 1.0 / (1.0 + age_seconds / DAY_SECONDS)
            score = (
                RANK_RELEVANCE * similarity
                + RANK_CONFIDENCE * float(memory.confidence)
                + RANK_RECENCY * recency
            )
            ranked.append(
                RankedMemory(memory=memory, score=score, match_reason=f"semantic match: {query}")
            )
        ranked.sort(key=lambda item: (-item.score, -item.memory.created_at.timestamp()))
        total = len(ranked)
        if limit is not None:
            ranked = ranked[offset : offset + limit]
        else:
            ranked = ranked[offset:]
        self._publish(
            MEMORY_RETRIEVED,
            {
                "query": query,
                "count": len(ranked),
                "total": total,
                "ids": [item.memory.id for item in ranked][:20],
                "session_id": filters.session_id,
                "mode": "semantic",
            },
        )
        return MemoryRetrieval(items=ranked, total=total)

    def _rank(self, memories: list[Memory], reason: str) -> list[RankedMemory]:
        now = utcnow()
        ranked: list[RankedMemory] = []
        for memory in memories:
            age_seconds = max((now - memory.created_at).total_seconds(), 0.0)
            recency = 1.0 / (1.0 + age_seconds / DAY_SECONDS)
            score = (
                RANK_RELEVANCE * 1.0
                + RANK_CONFIDENCE * float(memory.confidence)
                + RANK_RECENCY * recency
            )
            ranked.append(RankedMemory(memory=memory, score=score, match_reason=reason))
        ranked.sort(key=lambda item: (-item.score, -item.memory.created_at.timestamp()))
        return ranked

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher(
                Event(
                    type=event_type,
                    source="memory",
                    session_id=payload.get("session_id"),
                    payload=payload,
                )
            )
        except Exception as exc:
            log.warning("memory event publish failed: %s", exc, extra={"component": "memory"})
