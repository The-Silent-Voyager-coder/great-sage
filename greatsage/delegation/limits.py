"""Delegation limits: conservative defaults and absolute safety ceilings.

Phase 5B bounds every delegated task. Defaults are conservative and fully
configurable via the ``delegation`` configuration section, but the ceilings
below are hard constants: configuration validation rejects values above them
and the DelegationManager re-validates every task against the effective
limits (defense in depth, spec §13-14). Limits are never unlimited.
"""

from __future__ import annotations

from greatsage.agent.limits import check_bounded  # noqa: F401  (reused, not duplicated)

MAX_WALL_TIME_SECONDS_DEFAULT = 1800.0  # 30 minutes
MAX_WALL_TIME_SECONDS_CEILING = 7200.0  # 2 hours

MAX_OUTPUT_BYTES_DEFAULT = 4 * 1024 * 1024  # 4 MiB
MAX_OUTPUT_BYTES_CEILING = 32 * 1024 * 1024  # 32 MiB

MAX_PERMISSION_REQUESTS_DEFAULT = 50
MAX_PERMISSION_REQUESTS_CEILING = 200

MAX_SESSION_COUNT_DEFAULT = 3
MAX_SESSION_COUNT_CEILING = 10

MAX_DELEGATION_DEPTH_DEFAULT = 1
MAX_DELEGATION_DEPTH_CEILING = 1  # depth is capped at 1; never recursive

RECONNECT_LIMIT = 3  # bounded reconnect attempts; never an infinite loop
SSE_RECONNECT_LIMIT = RECONNECT_LIMIT  # legacy alias (pre-poll naming)

#: Pause between executor event sweeps. Poll-based providers (e.g. opencode
#: v2) answer each `iter_session_events` call with one bounded sweep; without
#: a pause the monitor would hammer HTTP back-to-back for the whole run.
POLL_INTERVAL_SECONDS = 2.0
