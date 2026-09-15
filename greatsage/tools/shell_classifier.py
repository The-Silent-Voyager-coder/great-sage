"""Shell command classification (spec §26).

Commands are classified SAFE / RESTRICTED / DANGEROUS / FORBIDDEN with
deterministic allowlist-style tables plus argument analysis for the two
arbitrary-execution executables (python, git). Unknown commands default to
RESTRICTED — this is a conservative default, not a blacklist pretending to
be secure. Tables are documented in docs/TOOLS.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import PureWindowsPath


class CommandClass(StrEnum):
    SAFE = "safe"
    RESTRICTED = "restricted"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


FORBIDDEN_COMMANDS = frozenset(
    {
        "format",
        "fdisk",
        "diskpart",
        "bcdedit",
        "bootrec",
        "mbr2gpt",
        "sfc",
        "dism",
        "regedit",
        "gpedit",
        "gpupdate",
        "auditpol",
        "secedit",
        "defrag",
        "chkdsk",
        "manage-bde",
        "repair-bde",
        "vssadmin",
    }
)

DANGEROUS_COMMANDS = frozenset(
    {
        "del",
        "erase",
        "rm",
        "rmdir",
        "rd",
        "taskkill",
        "tskill",
        "pskill",
        "kill",
        "pkill",
        "shutdown",
        "restart",
        "reboot",
        "halt",
        "poweroff",
        "reg",
        "wmic",
        "net",
        "sc",
        "mount",
        "umount",
        "mkfs",
        "fsck",
        "fuser",
        "pnputil",
        "netsh",
        "certutil",
        # OS-level installers: arbitrary code execution outside the project
        # (Phase 9: moved up from RESTRICTED so they always need approval).
        "msiexec",
        "winget",
        "choco",
        "scoop",
        # Living-off-the-land binaries: script hosts, downloaders, task
        # persistence, and ownership seizure (Phase 9).
        "mshta",
        "wscript",
        "cscript",
        "bitsadmin",
        "schtasks",
        "wevtutil",
        "regsvr32",
        "cmstp",
        "takeown",
    }
)

SHELL_LAUNCHERS = frozenset(
    {
        "cmd",
        "powershell",
        "pwsh",
        "bash",
        "sh",
        "zsh",
        "fish",
        "ksh",
        "csh",
        "wsl",
        "start",
        "rundll32",
    }
)

RESTRICTED_COMMANDS = frozenset(
    {
        "pip",
        "pip3",
        "uv",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "bun",
        "cargo",
        "go",
        "gem",
        "composer",
        "poetry",
        "conda",
        "mamba",
        "copy",
        "xcopy",
        "robocopy",
        "move",
        "rename",
        "ren",
        "mkdir",
        "attrib",
        "icacls",
        "cipher",
        "compact",
        "expand",
        "extrac32",
        "tar",
        "unzip",
        "zip",
        "7z",
        "curl",
        "wget",
        "git",
        "python",
        "python3",
        "pythonw",
        "py",
        "sqlite3",
        "mysql",
        "psql",
        "redis-cli",
    }
)

SAFE_COMMANDS = frozenset(
    {
        "where",
        "findstr",
        "echo",
        "ping",
        "ipconfig",
        "hostname",
        "netstat",
        "systeminfo",
        "ver",
        "whoami",
        "tasklist",
        "getmac",
        "nslookup",
        "tracert",
        "pathping",
        "date",
        "time",
        "pwd",
        "dir",
        "ls",
        "type",
        "cat",
        "tree",
        "fc",
        "comp",
        "sort",
        "more",
        "help",
        "chcp",
        "driverquery",
        "vol",
    }
)

_GIT_READ_ONLY = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "tag",
        # NOTE: `config` is deliberately NOT read-only (Phase 9): it can set
        # credential helpers and arbitrary repo config, so it stays RESTRICTED.
        "help",
        "ls-files",
        "rev-parse",
        "reflog",
        "describe",
        "merge-base",
        "shortlog",
    }
)
_GIT_DESTRUCTIVE = frozenset({"reset", "clean", "rm"})
_PYTHON_INFO_FLAGS = frozenset({"--version", "-V", "--help", "-h", "-VV"})


def _exe_name(command: Sequence[str]) -> str:
    name = PureWindowsPath(command[0]).name.casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _classify_git(rest: Sequence[str]) -> CommandClass:
    if not rest:
        return CommandClass.RESTRICTED
    subcommand = rest[0].casefold()
    if subcommand in _GIT_READ_ONLY:
        return CommandClass.SAFE
    if subcommand in _GIT_DESTRUCTIVE:
        return CommandClass.DANGEROUS
    return CommandClass.RESTRICTED


def _classify_python(rest: Sequence[str]) -> CommandClass:
    if not rest:
        return CommandClass.SAFE
    if all(flag in _PYTHON_INFO_FLAGS for flag in rest):
        return CommandClass.SAFE
    return CommandClass.RESTRICTED


#: Obfuscated-execution flags: base64-encoded PowerShell hides intent from
#: review, so it is forbidden outright (Phase 9). `-e` alone is NOT matched
#: (too many benign meanings); only the explicit encoded-command spellings.
_ENCODED_PS_FLAGS = frozenset({"-encodedcommand", "-enc", "-ec"})


def _has_encoded_flag(rest: Sequence[str]) -> bool:
    return any(
        isinstance(item, str) and item.casefold() in _ENCODED_PS_FLAGS
        for item in rest
    )


def classify_command(command: Sequence[str]) -> CommandClass:
    """Classify a command vector; conservative default for unknowns."""
    if not command:
        return CommandClass.FORBIDDEN
    exe = _exe_name(command)
    if exe in FORBIDDEN_COMMANDS:
        return CommandClass.FORBIDDEN
    if exe in ("powershell", "pwsh") and _has_encoded_flag(command[1:]):
        return CommandClass.FORBIDDEN
    if exe in SHELL_LAUNCHERS:
        return CommandClass.DANGEROUS
    if exe in DANGEROUS_COMMANDS:
        return CommandClass.DANGEROUS
    if exe in ("python", "python3", "pythonw", "py"):
        return _classify_python(command[1:])
    if exe == "git":
        return _classify_git(command[1:])
    if exe in SAFE_COMMANDS:
        return CommandClass.SAFE
    if exe in RESTRICTED_COMMANDS:
        return CommandClass.RESTRICTED
    return CommandClass.RESTRICTED
