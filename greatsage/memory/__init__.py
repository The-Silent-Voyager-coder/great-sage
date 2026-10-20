"""Great Sage memory subsystem (Phase 3 + roadmap B).

Persistent memory: working / long-term / episodic / semantic types, SQLite
persistence with provenance, confidence, expiration, deterministic
retrieval and ranking, session-scoped working memory, and optional local
embeddings (Ollama `nomic-embed-text`) powering `memory search --semantic`.

Submodules are imported lazily by the runtime to avoid import cycles; this
package intentionally exposes no eager imports.
"""
