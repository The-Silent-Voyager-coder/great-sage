"""Path security (spec §16-17): canonicalization, roots, protected files.

Never trust AI-supplied paths: every path argument is expanded, made
absolute against the explicit working directory, resolved, and checked
against the configured allowed/denied roots before a tool may use it.

The protected-file set is deliberately small and extensible (spec §15 —
a policy hook, not an enormous sensitive-file database).
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from greatsage.tools.environment import is_secret_name

PROTECTED_FILENAMES = frozenset(
    {
        # Previous generation (originals may still exist as backups).
        "memory.db",
        "audit.log",
        "jarvis.yaml",
        "jarvis.example.yaml",
        # Current generation.
        "sage-memory.db",
        "sage-workspace.db",
        "sage-plans.db",
        "sage-tasks.db",
        "sage-scheduler.db",
        "sage-audit.log",
        "sage.yaml",
        "sage.example.yaml",
        ".env",
        ".envrc",
    }
)

#: Committed templates that carry no secrets stay readable (docs/TOOLS.md).
PROTECTED_FILENAME_EXCEPTIONS = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)

#: SQLite sidecars of protected databases.
_DB_SIDECARS = frozenset({"-journal", "-wal", "-shm"})


def _is_protected_name(name: str) -> bool:
    """Filename check: exact names, `.env.*` variants, sqlite sidecars."""
    if name in PROTECTED_FILENAME_EXCEPTIONS:
        return False
    if name in PROTECTED_FILENAMES:
        return True
    if name.startswith(".env."):
        return True
    for suffix in _DB_SIDECARS:
        if name.endswith(suffix) and name[: -len(suffix)] in PROTECTED_FILENAMES:
            return True
    return False


def canonicalize(path: str, base: Path) -> Path:
    """Expand ~, make absolute against `base`, resolve symlinks if possible."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and not PureWindowsPath(path).is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def is_within(path: Path, root: Path) -> bool:
    """Containment check; case-insensitive on Windows (pathlib semantics)."""
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def is_protected_path(path: Path) -> bool:
    """True for the small documented protected set (memory db, audit log, …).

    Heuristic is filename-based: exact protected names plus secret-looking
    stems (e.g. `openai_api_key.txt`). Directories are never flagged.
    """
    if path.is_dir():
        return False
    name = path.name.casefold()
    if _is_protected_name(name):
        return True
    return is_secret_name(path.stem)
