"""Great Sage intelligence layer (Phase 2).

Provider-neutral core models, the AIProvider interface, provider registry,
deterministic router, Ollama/OpenCode adapters, mock provider, read-only
hardware benchmark, and the point-of-contact IntelligenceService facade.

This package keeps its `__init__` free of eager submodule imports to avoid
import cycles with `sage.core` (the runtime owns the service).
"""
