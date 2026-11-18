"""
Schema steps for the tables this fork adds to the library database.

These steps are versioned apart from `DB_SCHEMA_VERSION`, so the numbering of the upstream
migrations in `migrations.py` stays free of conflicts. The version lives in the same settings
table: a library database that gets reset loses it too, and every step then runs again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import ExternalID

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_FAVORITES,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_MEDIA_PROGRESS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
)
from music_assistant.helpers.release_groups import (
    GROUP_METADATA_COLUMNS,
    group_versions,
    shared_group_metadata,
    write_group_metadata,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable, Mapping

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


async def _share_release_group_metadata(database: DatabaseConnection) -> None:
    """Give every library version of an album the series and first year of its group."""
    rows = await database.get_rows_from_query(
        f"""SELECT {DB_TABLE_ALBUMS}.item_id AS item_id, {GROUP_METADATA_COLUMNS},
                (SELECT MIN(lookup.external_id) FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} lookup
                    WHERE lookup.media_type = 'album'
                    AND lookup.external_id_type = '{ExternalID.MB_RELEASEGROUP.value}'
                    AND lookup.item_id = {DB_TABLE_ALBUMS}.item_id) AS release_group_id
            FROM {DB_TABLE_ALBUMS}""",
        limit=0,
    )
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["release_group_id"]:
            groups.setdefault(row["release_group_id"], []).append(row)
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        for item_id, update in shared_group_metadata(group_versions(group_rows)).items():
            await write_group_metadata(database, item_id, update)


# the ids an album took over from the file of another album
_TAKEN_OVER_ID_TYPES: Final[tuple[ExternalID, ...]] = (
    ExternalID.MB_ALBUM,
    ExternalID.MB_RELEASEGROUP,
    ExternalID.BARCODE,
    ExternalID.DISCOGS,
    ExternalID.ASIN,
)


async def _delete_phantom_albums(database: DatabaseConnection) -> None:
    """Delete the albums on no source whose every track is also on another album."""
    rows = await database.get_rows_from_query(
        f"""SELECT albums.item_id AS item_id FROM {DB_TABLE_ALBUMS} albums
            WHERE NOT EXISTS (SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} mapping
                WHERE mapping.media_type = 'album' AND mapping.item_id = albums.item_id)
            AND EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_TRACKS} own
                WHERE own.album_id = albums.item_id)
            AND NOT EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_TRACKS} own
                WHERE own.album_id = albums.item_id
                AND NOT EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_TRACKS} other
                    WHERE other.track_id = own.track_id AND other.album_id != albums.item_id))""",
        limit=0,
    )
    for row in rows:
        album_id = int(row["item_id"])
        for table, match in (
            (DB_TABLE_ALBUM_TRACKS, {"album_id": album_id}),
            (DB_TABLE_ALBUM_ARTISTS, {"album_id": album_id}),
            (DB_TABLE_ALBUM_GROUP_DEFAULTS, {"album_id": album_id}),
            (DB_TABLE_EXTERNAL_ID_LOOKUP, {"media_type": "album", "item_id": album_id}),
            (DB_TABLE_FAVORITES, {"media_type": "album", "item_id": album_id}),
            (
                DB_TABLE_MEDIA_PROGRESS,
                {"media_type": "album", "item_id": str(album_id), "provider": "library"},
            ),
            (DB_TABLE_GENRE_MEDIA_ITEM_MAPPING, {"media_type": "album", "media_id": album_id}),
            (DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION, {"media_type": "album", "media_id": album_id}),
            (DB_TABLE_ALBUMS, {"item_id": album_id}),
        ):
            await database.delete(table, match)


async def _repair_albums_built_from_another_album(database: DatabaseConnection) -> None:
    """
    Undo what an album without a folder of its own took over from another album.

    Such an album (its id made up of artist and album name) could be refreshed from the file
    of a track merged with another album's track, and took that album's ids, cover and series,
    which then spread back to the other album through the release group they now shared. It
    shows as two release groups, one of them also on a differently named album. The ids they
    share go, and the cover and series of both albums are cleared to be collected again: the
    album's own ids come back from its own files.
    """
    await _delete_phantom_albums(database)
    rows = await database.get_rows_from_query(
        f"""SELECT albums.item_id AS item_id, other.item_id AS other_id
            FROM {DB_TABLE_ALBUMS} albums
            JOIN {DB_TABLE_EXTERNAL_ID_LOOKUP} own ON own.media_type = 'album'
                AND own.item_id = albums.item_id
                AND own.external_id_type = '{ExternalID.MB_RELEASEGROUP.value}'
            JOIN {DB_TABLE_EXTERNAL_ID_LOOKUP} shared ON shared.media_type = 'album'
                AND shared.external_id_type = own.external_id_type
                AND shared.external_id = own.external_id
                AND shared.item_id != albums.item_id
            JOIN {DB_TABLE_ALBUMS} other ON other.item_id = shared.item_id
                AND other.search_name != albums.search_name
            WHERE EXISTS (SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} mapping
                WHERE mapping.media_type = 'album' AND mapping.item_id = albums.item_id
                AND mapping.provider_domain LIKE 'filesystem%'
                AND (mapping.url IS NULL OR mapping.url = ''))
            AND (SELECT COUNT(DISTINCT groups.external_id)
                FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} groups
                WHERE groups.media_type = 'album' AND groups.item_id = albums.item_id
                AND groups.external_id_type = '{ExternalID.MB_RELEASEGROUP.value}') > 1""",
        limit=0,
    )
    others: dict[int, set[int]] = {}
    for row in rows:
        others.setdefault(int(row["item_id"]), set()).add(int(row["other_id"]))
    id_types = ", ".join(f"'{id_type.value}'" for id_type in _TAKEN_OVER_ID_TYPES)
    for album_id, other_ids in others.items():
        await database.execute_write(
            f"""DELETE FROM {DB_TABLE_EXTERNAL_ID_LOOKUP}
                WHERE media_type = 'album' AND item_id = :album_id
                AND external_id_type IN ({id_types})
                AND EXISTS (SELECT 1 FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} other
                    WHERE other.media_type = 'album'
                    AND other.item_id IN ({", ".join(str(other) for other in other_ids)})
                    AND other.external_id_type = {DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id_type
                    AND other.external_id = {DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id)""",
            {"album_id": album_id},
        )
    # the shared group gave the album the other's earliest first release year, never back
    for album_id in others:
        await _clear_collected_metadata(database, album_id, "'$.original_year'")
    for album_id in set().union(*others.values()) - others.keys():
        await _clear_collected_metadata(database, album_id)


async def _clear_collected_metadata(
    database: DatabaseConnection, album_id: int, *extra_paths: str
) -> None:
    """
    Clear the cover and series of an album, so the metadata scan collects them again.

    :param database: The library database.
    :param album_id: The library id of the album.
    :param extra_paths: Further metadata JSON paths to remove.
    """
    paths = ", ".join(("'$.last_refresh'", "'$.last_musicbrainz_lookup'", *extra_paths))
    await database.execute_write(
        f"""UPDATE {DB_TABLE_ALBUMS}
            SET metadata = json_remove(
                json_set(metadata, '$.images', json('[]'), '$.collections', json('[]')), {paths})
            WHERE item_id = :album_id""",
        {"album_id": album_id},
    )


# append only: the position of a step is its version number
_STEPS: Final[tuple[Callable[[DatabaseConnection], Awaitable[None]], ...]] = (
    _create_album_group_defaults,
    _add_album_track_disc_subtitle,
    _create_album_series_hidden,
    _share_release_group_metadata,
    _repair_albums_built_from_another_album,
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
