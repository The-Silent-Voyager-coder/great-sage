"""Allow `python -m greatsage ...` (standard CLI entry)."""

from greatsage.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
