"""
Schema steps for the tables this fork adds to the library database.

These steps are versioned apart from `DB_SCHEMA_VERSION`, so the numbering of the upstream
migrations in `migrations.py` stays free of conflicts. The version lives in the same settings
table: a library database that gets reset loses it too, and every step then runs again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import MediaItemCollection

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_SETTINGS,
)
from music_assistant.helpers.collections import share_collections
from music_assistant.helpers.json import json_dumps, json_loads

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from music_assistant.helpers.database import DatabaseConnection

CUSTOM_SCHEMA_VERSION_KEY: Final[str] = "custom_schema_version"
DB_TABLE_ALBUM_GROUP_DEFAULTS: Final[str] = "album_release_group_defaults"
DB_TABLE_ALBUM_SERIES_HIDDEN: Final[str] = "album_series_hidden"


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


async def _create_album_series_hidden(database: DatabaseConnection) -> None:
    """Create the table holding the album series hidden from the series list."""
    await database.execute(
        f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_SERIES_HIDDEN}(
                [title] TEXT PRIMARY KEY);"""
    )


async def _share_release_group_series(database: DatabaseConnection) -> None:
    """Give every library version of an album the series of all the versions of its group."""
    rows = await database.get_rows_from_query(
        f"""SELECT {DB_TABLE_ALBUMS}.item_id AS item_id,
                json_extract({DB_TABLE_ALBUMS}.metadata, '$.collections') AS collections,
                (SELECT MIN(lookup.external_id) FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} lookup
                    WHERE lookup.media_type = 'album'
                    AND lookup.external_id_type = '{ExternalID.MB_RELEASEGROUP.value}'
                    AND lookup.item_id = {DB_TABLE_ALBUMS}.item_id) AS release_group_id
            FROM {DB_TABLE_ALBUMS}""",
        limit=0,
    )
    groups: dict[str, dict[int, list[MediaItemCollection]]] = {}
    for row in rows:
        if not row["release_group_id"]:
            continue
        groups.setdefault(row["release_group_id"], {})[int(row["item_id"])] = [
            MediaItemCollection.from_dict(entry) for entry in json_loads(row["collections"] or "[]")
        ]
    for versions in groups.values():
        if len(versions) < 2:
            continue
        for item_id, collections in share_collections(versions).items():
            await database.execute(
                f"UPDATE {DB_TABLE_ALBUMS} SET metadata = "
                "json_set(metadata, '$.collections', json(:collections)) "
                "WHERE item_id = :item_id",
                {
                    "collections": json_dumps([entry.to_dict() for entry in collections]),
                    "item_id": item_id,
                },
            )


# append only: the position of a step is its version number
_STEPS: Final[tuple[Callable[[DatabaseConnection], Awaitable[None]], ...]] = (
    _create_album_group_defaults,
    _add_album_track_disc_subtitle,
    _create_album_series_hidden,
    _share_release_group_series,
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
