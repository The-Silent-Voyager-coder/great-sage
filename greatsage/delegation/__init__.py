"""Controlled delegation layer (Phase 5B): provider-neutral task offload.

Great Sage remains the authority: it owns delegation, security, permissions,
lifecycle, cancellation, audit, and task state. The delegated executor (e.g.
OpenCode) owns coding/task execution, sessions, provider execution,
progress/events, and diffs/results.

Modules import each other directly; this package init stays docstring-only to
avoid import cycles (the OpenCode adapter imports ``delegation.models``).
"""
