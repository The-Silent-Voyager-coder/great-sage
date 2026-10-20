"""Provider-neutral memory models.

The four memory categories, the typed Memory entry, retrieval filters, and
the deterministic ranking result. Embeddings live in `embeddings.py` +
`service.py` (roadmap B); no LLM extraction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from greatsage.exceptions import MemoryValidationError

# Content may be plain text (the common case, FTS-searchable) or structured
# JSON data (dict/list). Structured content is stored as JSON and indexed by
# its textual representation.
MemoryContent = str | dict[str, Any] | list[Any]


class MemoryType(StrEnum):
    WORKING = "working"
    LONG_TERM = "long_term"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class Provenance(StrEnum):
    """Canonical provenance kinds (extensible: any non-empty string is valid)."""

    USER_EXPLICIT = "user_explicit"
    SYSTEM_EVENT = "system_event"
    TOOL_RESULT = "tool_result"
    DOCUMENT = "document"
    AGENT_RESULT = "agent_result"
    IMPORT = "import"


def new_memory_id() -> str:
    """Robust unique memory identity (UUID-based, not sequential)."""
    return f"mem_{uuid.uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_expired(memory: Memory, now: datetime | None = None) -> bool:
    """True when the memory carries an expiration in the past."""
    if memory.expires_at is None:
        return False
    return memory.expires_at <= (now or utcnow())


def content_text(content: MemoryContent) -> str:
    """Textual representation of content, used for FTS indexing and display."""
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True, ensure_ascii=True)


@dataclass(frozen=True)
class Memory:
    """A single persistent memory entry (Phase 3 spec §4)."""

    id: str
    memory_type: MemoryType
    content: MemoryContent
    source: str
    provenance: str
    confidence: float
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    deleted_at: datetime | None = None

    def validate(self) -> None:
        """Validate invariants; raises MemoryValidationError on violation."""
        if not self.id:
            raise MemoryValidationError("memory id must not be empty")
        if not isinstance(self.memory_type, MemoryType):
            raise MemoryValidationError(
                f"invalid memory type: {self.memory_type!r} (expected one of "
                f"{sorted(t.value for t in MemoryType)})"
            )
        if not _valid_content(self.content):
            raise MemoryValidationError("memory content must be non-empty")
        try:
            json.dumps(self.content)
        except (TypeError, ValueError) as exc:
            raise MemoryValidationError("memory content must be JSON-serializable") from exc
        if not self.source or not str(self.source).strip():
            raise MemoryValidationError("memory source must be a non-empty string")
        if not self.provenance or not str(self.provenance).strip():
            raise MemoryValidationError("memory provenance must be a non-empty string")
        if len(self.provenance) > 100:
            raise MemoryValidationError("memory provenance must be at most 100 characters")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise MemoryValidationError(f"confidence must be a number, got {self.confidence!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise MemoryValidationError(
                f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if not isinstance(value, datetime):
                raise MemoryValidationError(f"{name} must be a datetime")
            if value.tzinfo is None:
                raise MemoryValidationError(f"{name} must be timezone-aware (UTC)")
        if self.expires_at is not None:
            if not isinstance(self.expires_at, datetime):
                raise MemoryValidationError("expires_at must be a datetime")
            if self.expires_at.tzinfo is None:
                raise MemoryValidationError("expires_at must be timezone-aware (UTC)")
        try:
            json.dumps(dict(self.metadata))
        except (TypeError, ValueError) as exc:
            raise MemoryValidationError("metadata must be JSON-serializable") from exc

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """JSON-safe representation (CLI/API output)."""
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.memory_type.value,
            "source": self.source,
            "provenance": self.provenance,
            "confidence": round(float(self.confidence), 4),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": dict(self.metadata),
            "session_id": self.session_id,
        }
        if include_content:
            data["content"] = self.content
        return data


def _valid_content(content: MemoryContent) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (dict, list)):
        return bool(content)
    return True


@dataclass(frozen=True)
class MemoryFilter:
    """Retrieval filters (§19): type/source/provenance/created/expiry/confidence."""

    memory_type: MemoryType | None = None
    source: str | None = None
    provenance: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    expires_before: datetime | None = None
    minimum_confidence: float | None = None
    session_id: str | None = None
    include_expired: bool = False
    include_deleted: bool = False

    def validate(self) -> None:
        if self.minimum_confidence is not None and not (
            0.0 <= self.minimum_confidence <= 1.0
        ):
            raise MemoryValidationError(
                f"minimum_confidence must be between 0.0 and 1.0, got {self.minimum_confidence}"
            )
        for name, value in (
            ("created_after", self.created_after),
            ("created_before", self.created_before),
            ("expires_before", self.expires_before),
        ):
            if value is not None and (
                not isinstance(value, datetime) or value.tzinfo is None
            ):
                raise MemoryValidationError(
                    f"{name} must be a timezone-aware datetime"
                )


@dataclass(frozen=True)
class RankedMemory:
    """A retrieval result with the deterministic score and match reason (§22)."""

    memory: Memory
    score: float
    match_reason: str

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data = self.memory.to_dict(include_content=include_content)
        data["score"] = round(self.score, 4)
        data["match_reason"] = self.match_reason
        return data


@dataclass(frozen=True)
class MemoryRetrieval:
    """Ranked retrieval outcome: items + the total matching count."""

    items: list[RankedMemory]
    total: int
