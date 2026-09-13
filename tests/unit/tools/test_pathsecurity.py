"""Path security tests (spec §16-17, §39)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greatsage.tools.pathsecurity import (
    canonicalize,
    is_protected_path,
    is_within,
)


def test_canonicalize_absolute_stays() -> None:
    base = Path("C:/GREATSAGE/workspaces")
    path = canonicalize("C:/temp/foo", base)
    assert path == Path("C:/temp/foo").resolve()


def test_canonicalize_relative_against_base(tmp_path: Path) -> None:
    base = tmp_path / "workspace"
    base.mkdir()
    path = canonicalize("notes/readme.md", base)
    assert path == (base / "notes" / "readme.md").resolve()


def test_canonicalize_keeps_base_unqualified(tmp_path: Path) -> None:
    base = tmp_path / "workspace"
    base.mkdir()
    assert canonicalize("notes", base) == (base / "notes").resolve()


def test_is_within() -> None:
    root = Path("C:/GREATSAGE/workspaces")
    assert is_within(Path("C:/GREATSAGE/workspaces/project/a.txt"), root)
    assert is_within(Path("C:/GREATSAGE/workspaces"), root)
    assert not is_within(Path("C:/GREATSAGE/other/x"), root)
    assert not is_within(Path("C:/GREATSAGE/workspaces_extra/x"), root)


def test_is_within_relative_root(tmp_path: Path) -> None:
    root = tmp_path / "base"
    root.mkdir()
    assert is_within(tmp_path / "base" / "x", root)


@pytest.mark.parametrize(
    "name",
    [
        "memory.db",
        "audit.log",
        "jarvis.yaml",
        "jarvis.example.yaml",
        "sage-memory.db",
        "sage-audit.log",
        "sage.yaml",
        "sage.example.yaml",
        ".env",
    ],
)
def test_protected_filenames(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    assert is_protected_path(target)


@pytest.mark.parametrize(
    "name",
    ["memory.db-wal", "sage-memory.db-wal", "sage-memory.db-shm", "sage-tasks.db-journal"],
)
def test_protected_db_sidecars(tmp_path: Path, name: str) -> None:
    assert is_protected_path(tmp_path / name)


def test_secret_looking_stems_protected(tmp_path: Path) -> None:
    assert is_protected_path(tmp_path / "openai_api_key.txt")
    assert is_protected_path(tmp_path / "credentials.json")
    assert is_protected_path(tmp_path / "my-token.dat")


def test_ordinary_files_not_protected(tmp_path: Path) -> None:
    assert not is_protected_path(tmp_path / "readme.md")
    assert not is_protected_path(tmp_path / "monkey.txt")
    assert not is_protected_path(tmp_path / "author_notes.txt")


def test_directories_never_protected(tmp_path: Path) -> None:
    (tmp_path / "tokens").mkdir()
    assert not is_protected_path(tmp_path / "tokens")
