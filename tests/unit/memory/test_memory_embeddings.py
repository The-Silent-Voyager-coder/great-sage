"""Embedding provider + semantic recall tests (roadmap Phase B)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from greatsage.exceptions import MemoryUnavailableError, MemoryValidationError
from greatsage.memory.embeddings import OllamaEmbeddingProvider, cosine_similarity
from greatsage.memory.service import MemoryService


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _ok_vectors(vectors: list[list[float]]) -> _FakeResponse:
    return _FakeResponse(json.dumps({"embeddings": vectors}).encode())


def test_cosine() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == -1.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == -1.0


def test_provider_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _ok_vectors([[0.1, 0.2]]),
    )
    vectors = OllamaEmbeddingProvider("http://127.0.0.1:11434", "m").embed(["hi"])
    assert vectors == [[0.1, 0.2]]


def test_provider_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: object, timeout: object = None) -> _FakeResponse:
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(MemoryUnavailableError):
        OllamaEmbeddingProvider("http://127.0.0.1:11434", "m").embed(["hi"])


def test_provider_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(b'{"nope": 1}'),
    )
    with pytest.raises(MemoryUnavailableError):
        OllamaEmbeddingProvider("http://127.0.0.1:11434", "m").embed(["hi"])


class _FakeProvider:
    """Deterministic vectors: cat-ish vs dog-ish topics."""

    model = "fake"

    def __init__(self, *args: object, **kwargs: object) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "cat" in t.lower() else [0.0, 1.0] for t in texts]

    def health(self) -> dict[str, Any]:
        return {"available": True, "model": self.model, "dim": 2}


def _service_with_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True
) -> MemoryService:
    from greatsage.configuration.loader import load_config

    d = str(tmp_path).replace("\\", "/")
    cfg = tmp_path / "emb.yaml"
    cfg.write_text(
        f"""
memory:
  enabled: true
  database_path: "{d}/sage-memory.db"
  auto_save_conversations: false
  default_confidence: 0.8
  retention_days: 365
  embeddings_enabled: {"true" if enabled else "false"}
  embedding_model: "fake"
  embedding_base_url: "http://127.0.0.1:11434"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("greatsage.memory.service.OllamaEmbeddingProvider", _FakeProvider)
    service = MemoryService()
    service.start(load_config(cfg).config)
    return service


def test_semantic_disabled_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_embeddings(tmp_path, monkeypatch, enabled=False)
    try:
        memory = service.remember("the cat sat")
        assert service._repository is not None
        assert service._repository.get_embedding(memory.id) is None  # never embedded
        with pytest.raises(MemoryValidationError):
            service.retrieve("cat", semantic=True)
        with pytest.raises(MemoryValidationError):
            service.reindex_embeddings()
    finally:
        service.shutdown()


def test_embed_on_save_and_semantic_rank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_embeddings(tmp_path, monkeypatch)
    try:
        cat = service.remember("the cat sat on the mat")
        service.remember("dogs bark loudly at night")
        assert service._repository is not None
        stored = service._repository.get_embedding(cat.id)
        assert stored is not None and stored[0] == "fake"
        result = service.retrieve("my cat", semantic=True)
        assert result.total == 1  # zero-similarity rows are excluded
        assert result.items[0].memory.id == cat.id
        assert result.items[0].match_reason.startswith("semantic match:")
        assert result.items[0].score > 0.0
    finally:
        service.shutdown()


def test_semantic_respects_text_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The vector path must honor the same filters as text search (parity):
    # a `--semantic` query may never surface rows the text path would hide.
    service = _service_with_embeddings(tmp_path, monkeypatch)
    try:
        keep = service.remember("the cat sat", source="alpha", confidence=0.9)
        service.remember("the cat sleeps", source="beta", confidence=0.9)
        service.remember("the cat runs", source="alpha", confidence=0.2)
        result = service.retrieve(
            "my cat", semantic=True, source="alpha", minimum_confidence=0.5
        )
        assert [item.memory.id for item in result.items] == [keep.id]
        none = service.retrieve("my cat", semantic=True, provenance="import")
        assert none.total == 0
    finally:
        service.shutdown()


def test_forget_drops_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_embeddings(tmp_path, monkeypatch)
    try:
        memory = service.remember("the cat sat")
        service.forget(memory.id)
        assert service._repository is not None
        assert service._repository.get_embedding(memory.id) is None
    finally:
        service.shutdown()


def test_reindex_backfills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_embeddings(tmp_path, monkeypatch, enabled=False)
    try:
        first = service.remember("the cat sat")
    finally:
        service.shutdown()
    enabled = _service_with_embeddings(tmp_path, monkeypatch, enabled=True)
    try:
        assert enabled._repository is not None
        assert enabled._repository.get_embedding(first.id) is None
        report = enabled.reindex_embeddings()
        assert report == {"model": "fake", "embedded": 1, "failed": 0, "skipped": 0}
        assert enabled._repository.get_embedding(first.id) is not None
    finally:
        enabled.shutdown()
