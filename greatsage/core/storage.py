"""Local storage: ensure the configured data directories exist and are writable.

Phase 1 only lays down the directory skeleton from config `core.*`; actual
data files (SQLite etc.) belong to later phases.
"""

from __future__ import annotations

import logging
from pathlib import Path

from greatsage.configuration.model import CoreConfig

log = logging.getLogger("greatsage.core.storage")


class StorageManager:
    """Creates and probes the configured local storage root directories."""

    def __init__(self, core: CoreConfig) -> None:
        self._core = core

    @property
    def data_dir(self) -> Path:
        return self._core.data_dir

    def ensure_directories(self) -> None:
        """Create every configured directory (idempotent)."""
        for directory in self._core.directories:
            directory.mkdir(parents=True, exist_ok=True)

    def probe_writable(self) -> bool:
        """True if the data root can be created/accessed and written to."""
        try:
            self._core.data_dir.mkdir(parents=True, exist_ok=True)
            probe = self._core.data_dir / ".sage-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError as exc:
            log.error(
                f"storage root not writable: {self._core.data_dir}",
                exc_info=exc,
                extra={"component": "storage"},
            )
            return False
