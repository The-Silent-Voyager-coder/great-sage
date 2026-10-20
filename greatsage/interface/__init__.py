"""Great Sage HUD interface (Phase 10).

Local-first, zero-cost, read-only status/dashboard over the existing
subsystem facades. The HUD is a leaf consumer: it aggregates health,
memory, agent, delegation, workspace, planning, and task read paths and
performs no actions.
"""

from greatsage.interface.formatting import format_dashboard, format_status
from greatsage.interface.models import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    VALID_SECTIONS,
    HudSnapshot,
    SectionSnapshot,
)
from greatsage.interface.service import HudService

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "VALID_SECTIONS",
    "HudService",
    "HudSnapshot",
    "SectionSnapshot",
    "format_dashboard",
    "format_status",
]
