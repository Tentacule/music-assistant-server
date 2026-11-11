"""Tests for the library album listings deduplicated by release group."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import MediaCollection

from music_assistant.constants import DB_TABLE_ALBUMS, DB_TABLE_SETTINGS
from music_assistant.controllers.music.custom_migrations import (
    CUSTOM_SCHEMA_VERSION_KEY,
    DB_TABLE_ALBUM_GROUP_DEFAULTS,
    DB_TABLE_ALBUM_SERIES_HIDDEN,
    migrate_custom_schema,
)
from music_assistant.helpers.json import json_dumps

from .helpers import create_album

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

GROUP_A = "aaaaaaaa-0000-0000-0000-000000000001"
GROUP_B = "bbbbbbbb-0000-0000-0000-000000000002"


async def _add_album(
    mass: MusicAssistant,
    item_id: str,
    name: str,
    release_group: str | None,
    added: int,
) -> Album:
    """
    Add an album to the library with a known time of addition.

    :param mass: The Music Assistant instance.
    :param item_id: The album id on the provider.
    :param name: The album name.
    :param release_group: The MusicBrainz release group of the album, if any.
    :param added: The timestamp the album was added at.
    """
    external_ids = {(ExternalID.MB_RELEASEGROUP, release_group)} if release_group else None
    provider_album = create_album(
        "spotify_1",
        item_id,
        name=name,
        artist_item_id="artist_1",
        external_ids=external_ids,
    )
    for mapping in provider_album.provider_mappings:
        mapping.in_library = True
    album = await mass.music.albums.add_item_to_library(provider_album)
    await mass.music.database.update(
        DB_TABLE_ALBUMS, {"item_id": int(album.item_id)}, {"timestamp_added": added}
    )
    return album


async def _listed_names(mass: MusicAssistant, **kwargs: object) -> list[str]:
    """Return the sorted names of the deduplicated library listing."""
    return sorted(item.name for item in await mass.music.albums.album_group_items(**kwargs))


async def test_listing_keeps_the_version_added_last(music_mass: MusicAssistant) -> None:
    """A release group without a default is listed once, through its newest version."""
    await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)
    await _add_album(music_mass, "b1", "Beta", GROUP_B, 100)
    await _add_album(music_mass, "c1", "Gamma", None, 100)

    assert await _listed_names(music_mass) == ["Alpha Deluxe", "Beta", "Gamma"]
    assert await music_mass.music.albums.album_group_count() == 3
    assert await music_mass.music.albums.library_count() == 4
    assert len(await music_mass.music.albums.library_items()) == 4


async def test_default_version_wins_over_the_newest(music_mass: MusicAssistant) -> None:
    """The default version represents its group, and a new default replaces the old one."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    deluxe = await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)

    await music_mass.music.albums.set_album_group_default(standard.item_id)
    assert await _listed_names(music_mass) == ["Alpha Standard"]

    await music_mass.music.albums.set_album_group_default(deluxe.item_id)
    assert await _listed_names(music_mass) == ["Alpha Deluxe"]


async def test_filter_falls_back_to_a_matching_version(music_mass: MusicAssistant) -> None:
    """A search matching only a hidden version still lists its group through that version."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)
    await music_mass.music.albums.set_album_group_default(standard.item_id)

    assert await _listed_names(music_mass, search="Deluxe") == ["Alpha Deluxe"]


async def test_search_by_artist_lists_a_group_once(music_mass: MusicAssistant) -> None:
    """The artist fallback of a search does not add a second version of a listed group."""
    await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)

    assert await _listed_names(music_mass, search="Test Artist") == ["Alpha Deluxe"]


async def test_info_offers_the_default_only_with_several_versions(
    music_mass: MusicAssistant,
) -> None:
    """Setting a default is offered for a non-default album of a group with several versions."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    deluxe = await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)
    lone = await _add_album(music_mass, "b1", "Beta", GROUP_B, 100)
    unknown = await _add_album(music_mass, "c1", "Gamma", None, 100)
    albums = music_mass.music.albums

    assert (await albums.album_group_info(standard.item_id, "library")).can_set_default
    assert not (await albums.album_group_info(lone.item_id, "library")).can_set_default
    assert not (await albums.album_group_info(unknown.item_id, "library")).can_set_default
    assert not (await albums.album_group_info("a1", "spotify_1")).can_set_default

    await albums.set_album_group_default(deluxe.item_id)
    info = await albums.album_group_info(deluxe.item_id, "library")
    assert info.is_default
    assert info.default_item_id == deluxe.item_id
    assert not info.can_set_default
    assert (await albums.album_group_info(standard.item_id, "library")).can_set_default


async def test_default_needs_a_release_group(music_mass: MusicAssistant) -> None:
    """An album without a release group cannot become a default."""
    unknown = await _add_album(music_mass, "c1", "Gamma", None, 100)

    with pytest.raises(InvalidDataError):
        await music_mass.music.albums.set_album_group_default(unknown.item_id)


async def test_versions_keep_only_the_same_release_group(music_mass: MusicAssistant) -> None:
    """Other versions hold the hidden library versions and provider items of the same group."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)
    await _add_album(music_mass, "b1", "Alpha Single", GROUP_B, 100)
    same_group = create_album(
        "tidal_1",
        "t1",
        name="Alpha Remaster",
        external_ids={(ExternalID.MB_RELEASEGROUP, GROUP_A)},
    )
    other_group = create_album(
        "tidal_1",
        "t2",
        name="Alpha Live",
        external_ids={(ExternalID.MB_RELEASEGROUP, GROUP_B)},
    )
    unknown_group = create_album("tidal_1", "t3", name="Alpha Unknown")

    albums = music_mass.music.albums
    with (
        patch.object(
            type(albums),
            "versions",
            AsyncMock(return_value=[same_group, other_group, unknown_group]),
        ),
        patch.object(music_mass, "get_provider", Mock(return_value=None)),
    ):
        versions = await albums.album_group_versions(standard.item_id, "library")

    assert sorted(item.name for item in versions) == ["Alpha Deluxe", "Alpha Remaster"]


async def test_versions_resolve_a_barcode_once(music_mass: MusicAssistant) -> None:
    """A version's barcode gives its release group, and the answer comes from the cache after."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    barcode = "012345678905"
    candidate = create_album(
        "tidal_1",
        "t1",
        name="Alpha Remaster",
        external_ids={(ExternalID.BARCODE, barcode)},
    )
    release = Mock()
    release.release_group.id = GROUP_A
    musicbrainz = Mock()
    musicbrainz.get_releases_by_barcode = AsyncMock(return_value=[release])

    albums = music_mass.music.albums
    with (
        patch.object(type(albums), "versions", AsyncMock(return_value=[candidate])),
        patch.object(music_mass, "get_provider", Mock(return_value=musicbrainz)),
        patch.object(music_mass.cache, "get", AsyncMock(side_effect=[None, GROUP_A])),
        patch.object(music_mass.cache, "set", AsyncMock()) as cache_set,
    ):
        first = await albums.album_group_versions(standard.item_id, "library")
        second = await albums.album_group_versions(standard.item_id, "library")

    assert [item.name for item in first] == ["Alpha Remaster"]
    assert [item.name for item in second] == ["Alpha Remaster"]
    musicbrainz.get_releases_by_barcode.assert_awaited_once()
    cache_set.assert_awaited_once()


async def test_album_without_group_has_no_versions(music_mass: MusicAssistant) -> None:
    """An album whose release group is unknown lists no other versions."""
    unknown = await _add_album(music_mass, "c1", "Gamma", None, 100)
    albums = music_mass.music.albums

    with (
        patch.object(type(albums), "versions", AsyncMock(return_value=[])) as versions,
        patch.object(music_mass, "get_provider", Mock(return_value=None)),
    ):
        assert await albums.album_group_versions(unknown.item_id, "library") == []
    versions.assert_not_awaited()


async def test_artist_albums_collapse_library_versions(music_mass: MusicAssistant) -> None:
    """The albums of a library artist hold one version per release group."""
    standard = await _add_album(music_mass, "a1", "Alpha Standard", GROUP_A, 100)
    await _add_album(music_mass, "a2", "Alpha Deluxe", GROUP_A, 200)
    await _add_album(music_mass, "b1", "Beta", GROUP_B, 100)
    await music_mass.music.albums.set_album_group_default(standard.item_id)
    artist_id = standard.artists[0].item_id

    albums = await music_mass.music.albums.album_group_artist_albums(artist_id, "library")

    assert sorted(item.name for item in albums) == ["Alpha Standard", "Beta"]


async def test_artist_albums_group_and_filter_series(music_mass: MusicAssistant) -> None:
    """The albums of an artist can be narrowed to (no) series and grouped per series."""
    second = await _add_album(music_mass, "s2", "Saga Two", None, 100)
    first = await _add_album(music_mass, "s1", "Saga One", None, 100)
    await _add_album(music_mass, "x1", "Standalone", None, 100)
    for album, sequence in ((first, 1), (second, 2)):
        await music_mass.music.database.update(
            DB_TABLE_ALBUMS,
            {"item_id": int(album.item_id)},
            {"metadata": json_dumps({"collections": [{"title": "Saga", "sequence": sequence}]})},
        )
    artist_id = first.artists[0].item_id
    albums = music_mass.music.albums

    only_series = await albums.album_group_artist_albums(
        artist_id, "library", series_filter="series"
    )
    assert sorted(item.name for item in only_series) == ["Saga One", "Saga Two"]
    no_series = await albums.album_group_artist_albums(artist_id, "library", series_filter="albums")
    assert [item.name for item in no_series] == ["Standalone"]

    grouped = await albums.album_group_artist_albums(
        artist_id, "library", collapse_collections=True
    )
    assert sorted(item.name for item in grouped) == ["Saga", "Standalone"]
    saga = next(item for item in grouped if isinstance(item, MediaCollection))
    assert saga.item_id == "album___Saga"
    assert [item.name for item in saga.items] == ["Saga One", "Saga Two"]


async def _add_series(mass: MusicAssistant, title: str, *names: str) -> None:
    """
    Add albums to the library as the albums of one series, in the given order.

    :param mass: The Music Assistant instance.
    :param title: The title of the series.
    :param names: The names of the albums of the series.
    """
    for sequence, name in enumerate(names, start=1):
        album = await _add_album(mass, f"{title}-{sequence}", name, None, 100)
        await mass.music.database.update(
            DB_TABLE_ALBUMS,
            {"item_id": int(album.item_id)},
            {"metadata": json_dumps({"collections": [{"title": title, "sequence": sequence}]})},
        )


async def test_series_list_holds_the_series_only(music_mass_with_cache: MusicAssistant) -> None:
    """The series list holds one item per series, and no album outside a series."""
    await _add_series(music_mass_with_cache, "Saga", "Saga One", "Saga Two")
    await _add_series(music_mass_with_cache, "Cycle", "Cycle One")
    await _add_album(music_mass_with_cache, "x1", "Standalone", None, 100)

    series = await music_mass_with_cache.music.albums.album_series()

    assert sorted(item.name for item in series) == ["Cycle", "Saga"]
    saga = next(item for item in series if item.name == "Saga")
    assert [item.name for item in saga.items] == ["Saga One", "Saga Two"]


async def test_hidden_series_leave_the_series_list(music_mass_with_cache: MusicAssistant) -> None:
    """A hidden series moves to the hidden list, and the album listings keep it."""
    await _add_series(music_mass_with_cache, "Saga", "Saga One", "Saga Two")
    await _add_series(music_mass_with_cache, "Cycle", "Cycle One")
    albums = music_mass_with_cache.music.albums

    await albums.set_album_series_hidden("album___Saga", True)

    assert [item.name for item in await albums.album_series()] == ["Cycle"]
    assert [item.name for item in await albums.album_series(hidden=True)] == ["Saga"]
    assert await albums.hidden_album_series() == ["album___Saga"]
    grouped = await albums.album_group_items(collapse_collections=True)
    assert sorted(item.name for item in grouped) == ["Cycle", "Saga"]

    await albums.set_album_series_hidden("album___Saga", False)

    assert sorted(item.name for item in await albums.album_series()) == ["Cycle", "Saga"]
    assert await albums.album_series(hidden=True) == []


async def test_series_list_pages(music_mass_with_cache: MusicAssistant) -> None:
    """The series list pages over the series, not over their albums."""
    await _add_series(music_mass_with_cache, "Alpha", "Alpha One", "Alpha Two")
    await _add_series(music_mass_with_cache, "Beta", "Beta One")
    albums = music_mass_with_cache.music.albums

    first = await albums.album_series(limit=1)
    second = await albums.album_series(limit=1, offset=1)

    assert len(first) == len(second) == 1
    assert {first[0].name, second[0].name} == {"Alpha", "Beta"}


async def test_custom_migration_is_idempotent(music_mass: MusicAssistant) -> None:
    """The custom steps ran at setup, and running them again keeps the stored rows."""
    database = music_mass.music.database
    row = await database.get_row(DB_TABLE_SETTINGS, {"key": CUSTOM_SCHEMA_VERSION_KEY})
    assert row is not None
    await database.insert_or_replace(
        DB_TABLE_ALBUM_GROUP_DEFAULTS, {"release_group_id": GROUP_A, "album_id": 1}
    )
    await database.insert_or_replace(DB_TABLE_ALBUM_SERIES_HIDDEN, {"title": "Saga"})
    await database.delete(DB_TABLE_SETTINGS, {"key": CUSTOM_SCHEMA_VERSION_KEY})

    await migrate_custom_schema(database, Mock())

    assert await database.get_row(DB_TABLE_ALBUM_GROUP_DEFAULTS, {"release_group_id": GROUP_A})
    assert await database.get_row(DB_TABLE_ALBUM_SERIES_HIDDEN, {"title": "Saga"})
    assert await database.get_row(DB_TABLE_SETTINGS, {"key": CUSTOM_SCHEMA_VERSION_KEY})


async def test_custom_migration_failure_does_not_raise() -> None:
    """A failing step is logged and leaves the version untouched."""
    database = Mock()
    database.get_row = AsyncMock(return_value=None)
    database.execute = AsyncMock(side_effect=RuntimeError("disk full"))
    database.insert_or_replace = AsyncMock()
    logger = Mock()

    await migrate_custom_schema(database, logger)

    logger.exception.assert_called_once()
    database.insert_or_replace.assert_not_awaited()
