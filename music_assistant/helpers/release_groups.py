"""Share the metadata of a release group between the library versions of an album."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.media_items import MediaItemCollection

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.helpers.collections import share_collections
from music_assistant.helpers.json import json_dumps, json_loads

if TYPE_CHECKING:
    from music_assistant.helpers.database import DatabaseConnection

# the metadata columns a release group query selects for each version
GROUP_METADATA_COLUMNS: Final[str] = (
    f"json_extract({DB_TABLE_ALBUMS}.metadata, '$.collections') AS collections, "
    f"json_extract({DB_TABLE_ALBUMS}.metadata, '$.original_year') AS original_year"
)


def group_versions(
    rows: list[Mapping[str, Any]],
) -> dict[int, tuple[list[MediaItemCollection], int | None]]:
    """
    Return the series and first release year of each version, by library id.

    :param rows: Rows with item_id and the GROUP_METADATA_COLUMNS.
    """
    return {
        int(row["item_id"]): (
            [MediaItemCollection.from_dict(entry) for entry in json_loads(row["collections"] or "[]")],
            row["original_year"],
        )
        for row in rows
    }


def shared_group_metadata(
    versions: Mapping[int, tuple[list[MediaItemCollection], int | None]],
) -> dict[int, dict[str, Any]]:
    """
    Return the metadata each version of a release group lacks of the group.

    The versions of one album are in the same series and were first released in the same
    year, so what one version knows holds for all: every series, and the earliest year.

    :param versions: The series and first release year of each version, by library id.
    """
    collections = share_collections({key: value[0] for key, value in versions.items()})
    years = [year for _, year in versions.values() if year]
    earliest = min(years) if years else None
    changes: dict[int, dict[str, Any]] = {}
    for key, (_, year) in versions.items():
        update: dict[str, Any] = {}
        if key in collections:
            update["collections"] = [entry.to_dict() for entry in collections[key]]
        if earliest and year != earliest:
            update["original_year"] = earliest
        if update:
            changes[key] = update
    return changes


async def write_group_metadata(
    database: DatabaseConnection, item_id: int, update: Mapping[str, Any]
) -> None:
    """
    Store shared release group metadata in the metadata of one library album.

    :param database: The library database.
    :param item_id: The library id of the album.
    :param update: The metadata fields to set, as shared_group_metadata returns them.
    """
    paths = ", ".join(f"'$.{name}', json(:{name})" for name in update)
    await database.execute_write(
        f"UPDATE {DB_TABLE_ALBUMS} SET metadata = json_set(metadata, {paths}) "
        "WHERE item_id = :item_id",
        {"item_id": item_id, **{name: json_dumps(value) for name, value in update.items()}},
    )
