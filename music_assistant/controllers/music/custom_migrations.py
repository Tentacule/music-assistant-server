"""
Schema steps for the tables this fork adds to the library database.

These steps are versioned apart from `DB_SCHEMA_VERSION`, so the numbering of the upstream
migrations in `migrations.py` stays free of conflicts. The version lives in the same settings
table: a library database that gets reset loses it too, and every step then runs again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant.constants import DB_TABLE_ALBUM_TRACKS, DB_TABLE_SETTINGS

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from music_assistant.helpers.database import DatabaseConnection

CUSTOM_SCHEMA_VERSION_KEY: Final[str] = "custom_schema_version"
DB_TABLE_ALBUM_GROUP_DEFAULTS: Final[str] = "album_release_group_defaults"


async def _create_album_group_defaults(database: DatabaseConnection) -> None:
    """Create the table holding the default version of a release group."""
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_GROUP_DEFAULTS}(
                [release_group_id] TEXT PRIMARY KEY,
                [album_id] INTEGER NOT NULL);"""
    )


async def _add_album_track_disc_subtitle(database: DatabaseConnection) -> None:
    """Add the disc name of a multi-disc album (discsubtitle tag) to its album tracks."""
    columns = {
        row["name"]
        for row in await database.get_rows_from_query(
            f"PRAGMA table_info({DB_TABLE_ALBUM_TRACKS})", limit=0
        )
    }
    if "disc_subtitle" not in columns:
        await database.execute(
            f"ALTER TABLE {DB_TABLE_ALBUM_TRACKS} ADD COLUMN disc_subtitle TEXT NOT NULL DEFAULT ''"
        )


# append only: the position of a step is its version number
_STEPS: Final[tuple[Callable[[DatabaseConnection], Awaitable[None]], ...]] = (
    _create_album_group_defaults,
    _add_album_track_disc_subtitle,
)


async def migrate_custom_schema(database: DatabaseConnection, logger: logging.Logger) -> None:
    """
    Run the custom schema steps the database has not had yet.

    A step that fails stops the run and is tried again on the next start, it never resets
    the library.

    :param database: The library database.
    :param logger: Logger to report a failed step on.
    """
    try:
        row = await database.get_row(DB_TABLE_SETTINGS, {"key": CUSTOM_SCHEMA_VERSION_KEY})
        current_version = int(row["value"]) if row else 0
    except KeyError, TypeError, ValueError:
        current_version = 0
    for version, step in enumerate(_STEPS, start=1):
        if version <= current_version:
            continue
        try:
            await step(database)
            await database.insert_or_replace(
                DB_TABLE_SETTINGS,
                {"key": CUSTOM_SCHEMA_VERSION_KEY, "value": str(version), "type": "str"},
            )
        except Exception:
            logger.exception("Custom database migration %s failed", version)
            return
