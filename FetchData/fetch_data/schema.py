"""Load the packaged SQL migrations that are the database schema source of truth."""

from importlib import resources
from typing import Iterable


def load_migration(name: str) -> str:
    """Read one immutable, package-owned SQL migration by file name."""

    return (
        resources.files("fetch_data.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def load_migrations(names: Iterable[str]) -> str:
    """Join migrations in caller-specified execution order."""

    return "\n\n".join(load_migration(name).strip() for name in names) + "\n"
