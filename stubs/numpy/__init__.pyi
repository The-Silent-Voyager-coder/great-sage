"""Minimal local stub for numpy (type-checking only, never imported at runtime).

numpy>=2.5 ships PEP-695 ``type``-statement stubs that mypy cannot parse
while targeting our Python 3.11 floor (``python_version`` in
``pyproject.toml``). Without this stub, ``mypy greatsage`` aborts with
``Type statement is only supported in Python 3.12 and greater`` and checks
nothing — locally and on every CI leg.

This stub keeps numpy attribute access dynamically typed (``Any``) so the
rest of the codebase stays fully checked. Runtime behavior is unchanged:
the real numpy is imported at runtime. If our numpy surface ever grows
beyond plain attribute calls, extend this stub with the names used.
See ``docs/DEPENDENCY_POLICY.md`` §5 (numpy row).
"""

from typing import Any


def __getattr__(name: str) -> Any: ...
