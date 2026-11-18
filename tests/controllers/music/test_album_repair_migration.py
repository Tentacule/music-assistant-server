"""Tests for the custom step repairing albums built from another album's file."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import ExternalID

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_SETTINGS,
)
from music_assistant.controllers.music.custom_migrations import (
    CUSTOM_SCHEMA_VERSION_KEY,
    migrate_custom_schema,
)
from music_assistant.helpers.json import json_dumps, json_loads

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

PROVIDER = "filesystem_1"
RG_NOW = "11111111-0000-0000-0000-000000000001"
RG_ACTUALLY = "22222222-0000-0000-0000-000000000002"
REL_NOW = "33333333-0000-0000-0000-000000000003"
REL_ACTUALLY = "44444444-0000-0000-0000-000000000004"
BARCODE_NOW = "724352027422"
BARCODE_ACTUALLY = "5099926829029"
COLLECTED: dict[str, Any] = {
    "images": [{"type": "thumb", "path": "x.jpg", "provider": PROVIDER}],
    "collections": [{"title": "1001 Albums"}, {"title": "Millennium Series"}],
    "original_year": 1987,
    "last_refresh": 123,
    "last_musicbrainz_lookup": 123,
}


async def _add_album(
    mass: MusicAssistant,
    item_id: str,
    name: str,
    ids: set[tuple[ExternalID, str]],
    folder: str | None = None,
) -> int:
    """
    Add a library album of the test provider with ids and collected metadata.

    :param mass: The Music Assistant instance.
    :param item_id: The album id on the provider.
    :param name: The album name.
    :param ids: The external ids to store on the album.
    :param folder: The album's own folder, None for an id made up of artist and album name.
    """
    # an artist of its own, so the library never matches two of them as one album
    provider_album = create_album(PROVIDER, item_id, name=name, artist_name=item_id)
    for mapping in provider_album.provider_mappings:
        mapping.in_library = True
    album_id = int((await mass.music.albums.add_item_to_library(provider_album)).item_id)
    database = mass.music.database
    # stored directly: through the controller, the shared ids would merge the albums
    for id_type, value in ids:
        await database.insert(
            DB_TABLE_EXTERNAL_ID_LOOKUP,
            {
                "media_type": "album",
                "external_id_type": id_type.value,
                "external_id": value,
                "item_id": album_id,
            },
        )
    await database.update(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": "album", "item_id": album_id},
        {"url": folder},
    )
    await database.update(
        DB_TABLE_ALBUMS, {"item_id": album_id}, {"metadata": json_dumps(COLLECTED)}
    )
    return album_id


async def _add_track(mass: MusicAssistant, item_id: str, album_ids: list[int]) -> None:
    """Add a library track on the given albums."""
    track = create_track(PROVIDER, item_id, isrc=f"GBAAA87000{item_id[-2:]}")
    for mapping in track.provider_mappings:
        mapping.in_library = True
    track_id = int((await mass.music.tracks.add_item_to_library(track)).item_id)
    for album_id in album_ids:
        await mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_TRACKS,
            {"track_id": track_id, "album_id": album_id, "disc_number": 1, "track_number": 1},
        )


async def _ids(mass: MusicAssistant, album_id: int) -> set[tuple[str, str]]:
    """Return the external ids stored on an album."""
    rows = await mass.music.database.get_rows(
        DB_TABLE_EXTERNAL_ID_LOOKUP, {"media_type": "album", "item_id": album_id}, limit=0
    )
    return {(row["external_id_type"], row["external_id"]) for row in rows}


async def _metadata(mass: MusicAssistant, album_id: int) -> dict[str, Any]:
    """Return the stored metadata of an album."""
    row = await mass.music.database.get_row(DB_TABLE_ALBUMS, {"item_id": album_id})
    assert row is not None
    metadata: dict[str, Any] = json_loads(row["metadata"])
    return metadata


async def _run_step(mass: MusicAssistant) -> None:
    """Run the custom steps from the one before the repair."""
    database = mass.music.database
    await database.insert_or_replace(
        DB_TABLE_SETTINGS, {"key": CUSTOM_SCHEMA_VERSION_KEY, "value": "4", "type": "str"}
    )
    await migrate_custom_schema(database, Mock())


async def test_repair_removes_the_other_albums_ids_and_metadata(
    music_mass: MusicAssistant,
) -> None:
    """A compilation loses the ids it took over, and both albums get their metadata again."""
    now = await _add_album(
        music_mass,
        "Various Artists/Now 1987",
        "Now 1987",
        {
            (ExternalID.MB_RELEASEGROUP, RG_NOW),
            (ExternalID.MB_RELEASEGROUP, RG_ACTUALLY),
            (ExternalID.MB_ALBUM, REL_NOW),
            (ExternalID.MB_ALBUM, REL_ACTUALLY),
            (ExternalID.BARCODE, BARCODE_NOW),
            (ExternalID.BARCODE, BARCODE_ACTUALLY),
        },
    )
    actually = await _add_album(
        music_mass,
        "Pet Shop Boys/Actually",
        "Actually",
        {
            (ExternalID.MB_RELEASEGROUP, RG_ACTUALLY),
            (ExternalID.MB_ALBUM, REL_ACTUALLY),
            (ExternalID.BARCODE, BARCODE_ACTUALLY),
        },
        folder="Pet Shop Boys/Actually",
    )

    await _run_step(music_mass)

    assert await _ids(music_mass, now) == {
        (ExternalID.MB_RELEASEGROUP.value, RG_NOW),
        (ExternalID.MB_ALBUM.value, REL_NOW),
        (ExternalID.BARCODE.value, BARCODE_NOW),
    }
    assert await _metadata(music_mass, now) == {"images": [], "collections": []}
    assert len(await _ids(music_mass, actually)) == 3
    assert await _metadata(music_mass, actually) == {
        "images": [],
        "collections": [],
        "original_year": 1987,
    }


async def test_repair_keeps_editions_and_albums_with_a_folder(
    music_mass: MusicAssistant,
) -> None:
    """Two editions of one album, and an album in its own folder, keep everything."""
    edition_ids = {
        (ExternalID.MB_RELEASEGROUP, RG_NOW),
        (ExternalID.MB_RELEASEGROUP, RG_ACTUALLY),
    }
    edition = await _add_album(music_mass, "Beatles/Abbey Road (2CD)", "Abbey Road", edition_ids)
    await _add_album(
        music_mass,
        "Beatles/Abbey Road",
        "Abbey Road",
        {(ExternalID.MB_RELEASEGROUP, RG_ACTUALLY)},
        folder="Beatles/Abbey Road",
    )
    # its second release group is also on a differently named album
    in_folder = await _add_album(
        music_mass,
        "VA/Hits",
        "Hits",
        {(ExternalID.MB_RELEASEGROUP, REL_NOW), (ExternalID.MB_RELEASEGROUP, REL_ACTUALLY)},
        folder="VA/Hits",
    )
    await _add_album(
        music_mass,
        "Pet Shop Boys/Actually",
        "Actually",
        {(ExternalID.MB_RELEASEGROUP, REL_ACTUALLY)},
        folder="Pet Shop Boys/Actually",
    )

    await _run_step(music_mass)

    for album_id in (edition, in_folder):
        assert len(await _ids(music_mass, album_id)) == 2
        assert await _metadata(music_mass, album_id) == COLLECTED


async def test_repair_deletes_a_phantom_album_only_when_its_tracks_live_on(
    music_mass: MusicAssistant,
) -> None:
    """An album on no source goes when its tracks are on another album, and stays otherwise."""
    now = await _add_album(music_mass, "Various Artists/Now 1987", "Now 1987", set())
    phantom = await _add_album(music_mass, "phantom", "Actually", set())
    alone = await _add_album(music_mass, "alone", "Lonely", set())
    database = music_mass.music.database
    for album_id in (phantom, alone):
        await database.delete(
            DB_TABLE_PROVIDER_MAPPINGS, {"media_type": "album", "item_id": album_id}
        )
    await _add_track(music_mass, "track_01", [now, phantom])
    await _add_track(music_mass, "track_02", [alone])

    await _run_step(music_mass)

    assert await database.get_row(DB_TABLE_ALBUMS, {"item_id": phantom}) is None
    assert not await database.get_rows(DB_TABLE_ALBUM_TRACKS, {"album_id": phantom})
    assert await database.get_row(DB_TABLE_ALBUMS, {"item_id": alone}) is not None
    assert await database.get_rows(DB_TABLE_ALBUM_TRACKS, {"album_id": now})
