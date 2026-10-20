"""Security policy (spec §10-11, §14-17): decisions and argument-aware hooks.

The policy evaluates tool + risk + arguments + session/source + configured
mode and returns a ToolDecision with a human-readable reason. Hooks can only
tighten decisions (DENY/ASK over ALLOW); they never loosen them.

Defaults (documented in docs/TOOLS.md):
  LOCKDOWN    : SAFE only
  NORMAL      : SAFE/LOW auto, MEDIUM/HIGH ask
  DEVELOPMENT : SAFE/LOW/MEDIUM auto, HIGH ask
  CRITICAL    : always DENY in every mode
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from greatsage.configuration.model import ToolSecurityMode
from greatsage.tools.environment import is_secret_name
from greatsage.tools.models import Tool, ToolDecision, ToolRequest, ToolRisk
from greatsage.tools.pathsecurity import canonicalize, is_protected_path, is_within
from greatsage.tools.redaction import looks_like_secret_value
from greatsage.tools.shell_classifier import CommandClass, classify_command

if TYPE_CHECKING:
    from greatsage.configuration.model import SageConfig


@runtime_checkable
class PolicyHook(Protocol):
    def check(
        self, request: ToolRequest, tool: Tool, decision: ToolDecision
    ) -> tuple[ToolDecision, str | None]:
        """Return (decision, reason). Reason is set only when tightened."""
        ...


class SensitiveArgumentHook:
    """Rejects credential-carrying arguments (spec §30, §32; Phase 9 values).

    Denial triggers (key-shaped *or* value-shaped, never ordinary prose):
    - an argument *name* that looks like a credential with a non-empty value;
    - an `env` mapping entry whose key looks secret-shaped or whose value
      matches a high-confidence secret format;
    - a `command` argv item matching a high-confidence secret format (secret
      material on a command line is visible to process listings and logs).
    """

    def check(
        self, request: ToolRequest, tool: Tool, decision: ToolDecision
    ) -> tuple[ToolDecision, str | None]:
        for name, value in request.arguments.items():
            if is_secret_name(name) and value not in (None, "", b""):
                return ToolDecision.DENY, (
                    f"argument {name!r} looks like a credential and is not allowed"
                )
        env = request.arguments.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                if isinstance(key, str) and is_secret_name(key):
                    return ToolDecision.DENY, (
                        f"env key {key!r} looks like a credential and is not allowed"
                    )
                if isinstance(value, str) and looks_like_secret_value(value):
                    return ToolDecision.DENY, (
                        "env value looks like a credential and is not allowed"
                    )
        command = request.arguments.get("command")
        if isinstance(command, list):
            for item in command:
                if isinstance(item, str) and looks_like_secret_value(item):
                    return ToolDecision.DENY, (
                        "command argument looks like a credential and is not allowed"
                    )
        return decision, None


class PathSecurityHook:
    """Argument-aware path enforcement (spec §15-17).

    Every declared path argument is canonicalized and checked against denied
    roots, allowed roots, and the protected-file set. Relative paths resolve
    against the explicit working directory.
    """

    def __init__(
        self,
        allowed_roots: tuple[Path, ...],
        denied_roots: tuple[Path, ...],
        working_directory: Path,
    ) -> None:
        self._allowed = allowed_roots
        self._denied = denied_roots
        self._working = working_directory

    def check(
        self, request: ToolRequest, tool: Tool, decision: ToolDecision
    ) -> tuple[ToolDecision, str | None]:
        for name in tool.PATH_ARGUMENTS:
            if name not in request.arguments:
                continue
            value = request.arguments[name]
            candidates = value if isinstance(value, list) else [value]
            for item in candidates:
                if not isinstance(item, str) or not item.strip():
                    continue
                path = canonicalize(item, self._working)
                for root in self._denied:
                    if is_within(path, root):
                        return ToolDecision.DENY, (
                            f"{name} {path} is inside a denied root: {root}"
                        )
                if not any(is_within(path, root) for root in self._allowed):
                    return ToolDecision.DENY, (
                        f"{name} {path} is outside allowed roots: "
                        + ", ".join(str(root) for root in self._allowed)
                    )
                # Protected files are denied on *every* declared path argument
                # (Phase 9): `shell.execute` declares `cwd`, not `path`, and a
                # name-scoped check would silently skip it.
                if is_protected_path(path):
                    return ToolDecision.DENY, (
                        f"{name} {path} targets a protected file"
                    )
        return decision, None


class ShellCommandHook:
    """Classification gate for shell.execute (spec §26).

    FORBIDDEN and DANGEROUS commands are always denied — the classifier is
    policy, not decoration. SAFE/RESTRICTED commands still need the tool's
    own risk approval (shell.execute is HIGH).
    """

    def check(
        self, request: ToolRequest, tool: Tool, decision: ToolDecision
    ) -> tuple[ToolDecision, str | None]:
        if tool.id != "shell.execute":
            return decision, None
        command = request.arguments.get("command")
        if not isinstance(command, list) or not command:
            return decision, None
        command_class = classify_command(command)
        if command_class in (CommandClass.FORBIDDEN, CommandClass.DANGEROUS):
            return ToolDecision.DENY, (
                f"command class {command_class.value}: {command[0]}"
            )
        return decision, None


def _risk_decision(
    risk: ToolRisk, mode: ToolSecurityMode, allow_low: bool
) -> ToolDecision:
    if risk is ToolRisk.CRITICAL:
        return ToolDecision.DENY
    if mode is ToolSecurityMode.LOCKDOWN:
        return ToolDecision.ALLOW if risk is ToolRisk.SAFE else ToolDecision.DENY
    if risk is ToolRisk.SAFE:
        return ToolDecision.ALLOW
    if risk is ToolRisk.LOW:
        return ToolDecision.ALLOW if allow_low else ToolDecision.ASK
    if mode is ToolSecurityMode.DEVELOPMENT and risk is ToolRisk.MEDIUM:
        return ToolDecision.ALLOW
    if risk in (ToolRisk.MEDIUM, ToolRisk.HIGH):
        return ToolDecision.ASK
    return ToolDecision.DENY  # unreachable; CRITICAL handled above


class SecurityPolicy:
    """Composes the mode-driven decision with argument-aware hooks."""

    def __init__(
        self,
        mode: ToolSecurityMode,
        *,
        allow_low: bool = True,
        hooks: tuple[PolicyHook, ...] = (),
    ) -> None:
        self._mode = mode
        self._allow_low = allow_low
        self._hooks = hooks

    @classmethod
    def from_config(cls, config: SageConfig) -> SecurityPolicy:
        tools = config.tools
        hooks = (
            SensitiveArgumentHook(),
            PathSecurityHook(tools.allowed_roots, tools.denied_roots, tools.working_directory),
            ShellCommandHook(),
        )
        return cls(
            config.security.mode,
            allow_low=config.security.allow_auto_approve_read,
            hooks=hooks,
        )

    @property
    def mode(self) -> ToolSecurityMode:
        return self._mode

    def evaluate(self, request: ToolRequest, tool: Tool) -> tuple[ToolDecision, str]:
        decision = _risk_decision(tool.risk_level, self._mode, self._allow_low)
        reasons: list[str] = []
        for hook in self._hooks:
            hook_decision, reason = hook.check(request, tool, decision)
            if hook_decision != decision:
                reasons.append(reason or "policy hook tightened the decision")
                decision = hook_decision
        if decision is ToolDecision.ASK:
            return decision, f"tool {tool.id} ({tool.risk_level.value}) requires approval"
        if decision is ToolDecision.DENY:
            reason = (
                reasons[-1]
                if reasons
                else f"tool {tool.id} ({tool.risk_level.value}) is denied "
                f"in mode {self._mode.value}"
            )
            return decision, reason
        return decision, "allowed"
