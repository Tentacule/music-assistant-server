"""Tests for the editing of the album series: types, own series and their albums."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import AlbumSeriesSource, AlbumSeriesType, ExternalID
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import MediaItemCollection

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.helpers.api import _get_type_hints_for_api_command, parse_arguments
from music_assistant.helpers.json import json_dumps

from .helpers import create_album

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

GROUP_A = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def mass(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Return a Music Assistant instance with a cache, which the series listings use."""
    return music_mass_with_cache


async def _add_album(
    mass: MusicAssistant, item_id: str, name: str, release_group: str | None = None
) -> Album:
    """Add an album to the library, in a release group when given."""
    external_ids = {(ExternalID.MB_RELEASEGROUP, release_group)} if release_group else None
    provider_album = create_album(
        "spotify_1", item_id, name=name, artist_item_id="artist_1", external_ids=external_ids
    )
    for mapping in provider_album.provider_mappings:
        mapping.in_library = True
    return await mass.music.albums.add_item_to_library(provider_album)


async def _set_series(mass: MusicAssistant, album: Album, title: str, sequence: int) -> None:
    """Store a series in the metadata of a library album, as MusicBrainz would."""
    await mass.music.database.update(
        DB_TABLE_ALBUMS,
        {"item_id": int(album.item_id)},
        {"metadata": json_dumps({"collections": [{"title": title, "sequence": sequence}]})},
    )


async def _series_of(mass: MusicAssistant, album: Album) -> list[tuple[str, object]]:
    """Return the series of a library album, with their number."""
    stored = await mass.music.albums.get_library_item(album.item_id)
    return [(entry.title, entry.sequence) for entry in stored.metadata.collections or []]


async def _names(mass: MusicAssistant, title: str, visibility: str = "visible") -> list[str]:
    """Return the names of the albums of a series, in series order."""
    collection = await mass.music.albums.get_collection(
        f"album___{title}", album_visibility=visibility
    )
    return [album.name for album in collection.items]


async def test_an_own_series_is_listed_while_empty(mass: MusicAssistant) -> None:
    """A new series of the user's own is listed with its type and source, without albums."""
    albums = mass.music.albums

    created = await albums.create_album_series(" Desert island ")

    assert created.item_id == "album___Desert island"
    assert created.metadata.series_type == AlbumSeriesType.PERSONAL
    assert created.metadata.series_source == AlbumSeriesSource.CUSTOM
    listed = await albums.album_series()
    assert [item.name for item in listed] == ["Desert island"]
    assert await _names(mass, "Desert island") == []
    with pytest.raises(InvalidDataError):
        await albums.create_album_series("Desert island")
    with pytest.raises(InvalidDataError):
        await albums.create_album_series("  ")


async def test_albums_go_into_an_own_series_by_number(mass: MusicAssistant) -> None:
    """Added albums follow their number, which can change, and leave the series for good."""
    albums = mass.music.albums
    await albums.create_album_series("Best of", AlbumSeriesType.RANKED_LIST)
    first = await _add_album(mass, "a1", "Alpha")
    second = await _add_album(mass, "b1", "Beta")

    await albums.add_album_series_album("album___Best of", first.item_id, "2")
    await albums.add_album_series_album("album___Best of", second.item_id, "1")
    assert await _names(mass, "Best of") == ["Beta", "Alpha"]
    assert await _series_of(mass, first) == [("Best of", 2)]

    await albums.add_album_series_album("album___Best of", first.item_id, "0.5")
    assert await _names(mass, "Best of") == ["Alpha", "Beta"]

    await albums.remove_album_series_album("album___Best of", first.item_id)
    assert await _names(mass, "Best of") == ["Beta"]
    assert await _names(mass, "Best of", "hidden") == []
    assert await _series_of(mass, first) == []
    listed = await albums.album_series(series_type=AlbumSeriesType.RANKED_LIST)
    assert [item.name for item in listed] == ["Best of"]
    assert await albums.album_series(series_type=AlbumSeriesType.ALBUM_SERIES) == []


async def test_an_album_removed_from_a_musicbrainz_series_stays_out(mass: MusicAssistant) -> None:
    """An album removed from a series of MusicBrainz is hidden there, even after a refresh."""
    albums = mass.music.albums
    first = await _add_album(mass, "r1", "Rock One")
    second = await _add_album(mass, "r2", "Rock Two")
    await _set_series(mass, first, "Reservoir Rock", 1)
    await _set_series(mass, second, "Reservoir Rock", 2)

    await albums.remove_album_series_album("album___Reservoir Rock", second.item_id)

    assert await _names(mass, "Reservoir Rock") == ["Rock One"]
    assert await _names(mass, "Reservoir Rock", "hidden") == ["Rock Two"]
    assert await _names(mass, "Reservoir Rock", "all") == ["Rock One", "Rock Two"]
    collection = await albums.get_collection("album___Reservoir Rock")
    assert collection.metadata.series_source == AlbumSeriesSource.MUSICBRAINZ
    assert collection.metadata.hidden_item_ids == [second.item_id]

    # a refresh of the metadata brings the series of MusicBrainz again
    refreshed = await albums.get_library_item(second.item_id)
    refreshed.metadata.collections = [MediaItemCollection(title="Reservoir Rock", sequence=2)]
    await albums.update_item_in_library(second.item_id, refreshed)
    assert await _series_of(mass, second) == []
    assert await _names(mass, "Reservoir Rock") == ["Rock One"]

    await albums.restore_album_series_album("album___Reservoir Rock", second.item_id)
    assert await _names(mass, "Reservoir Rock") == ["Rock One", "Rock Two"]
    assert await _series_of(mass, second) == [("Reservoir Rock", 2)]
    assert await _names(mass, "Reservoir Rock", "hidden") == []


async def test_a_series_with_every_album_removed_lists_them_as_hidden(mass: MusicAssistant) -> None:
    """A series of MusicBrainz whose albums were all removed still shows them as hidden."""
    albums = mass.music.albums
    only = await _add_album(mass, "r1", "Rock One")
    await _set_series(mass, only, "Reservoir Rock", 1)

    await albums.remove_album_series_album("album___Reservoir Rock", only.item_id)

    assert await _names(mass, "Reservoir Rock", "hidden") == ["Rock One"]
    assert await _names(mass, "Reservoir Rock") == []


async def test_every_version_leaves_and_stays_out_of_a_series(mass: MusicAssistant) -> None:
    """Removing an album removes its versions, and a version added later stays out too."""
    albums = mass.music.albums
    standard = await _add_album(mass, "a1", "Alpha Standard", GROUP_A)
    await _set_series(mass, standard, "Saga", 1)
    deluxe = await _add_album(mass, "a2", "Alpha Deluxe", GROUP_A)
    assert await _series_of(mass, deluxe) == [("Saga", 1)]

    await albums.remove_album_series_album("album___Saga", standard.item_id)
    assert await _series_of(mass, deluxe) == []

    remaster = await _add_album(mass, "a3", "Alpha Remaster", GROUP_A)
    remaster.metadata.collections = [MediaItemCollection(title="Saga", sequence=1)]
    await albums.update_item_in_library(remaster.item_id, remaster)
    assert await _series_of(mass, remaster) == []
    assert len(await _names(mass, "Saga", "hidden")) == 1


async def test_the_type_of_a_musicbrainz_series_can_change(mass: MusicAssistant) -> None:
    """A series of MusicBrainz keeps its source when its type is set, and cannot be deleted."""
    albums = mass.music.albums
    album = await _add_album(mass, "l1", "Listed")
    await _set_series(mass, album, "1001 Albums", 1)

    await albums.set_album_series_type("album___1001 Albums", AlbumSeriesType.RANKED_LIST)

    collection = await albums.get_collection("album___1001 Albums")
    assert collection.metadata.series_type == AlbumSeriesType.RANKED_LIST
    assert collection.metadata.series_source == AlbumSeriesSource.MUSICBRAINZ
    with pytest.raises(InvalidDataError):
        await albums.delete_album_series("album___1001 Albums")
    with pytest.raises(MediaNotFoundError):
        await albums.set_album_series_type("album___Nothing", AlbumSeriesType.RANKED_LIST)


async def test_deleting_an_own_series_frees_its_albums(mass: MusicAssistant) -> None:
    """Deleting a series of the user's own takes its albums out and removes it."""
    albums = mass.music.albums
    await albums.create_album_series("Mine")
    album = await _add_album(mass, "m1", "Mine One")
    await albums.add_album_series_album("album___Mine", album.item_id)

    await albums.delete_album_series("album___Mine")

    assert await _series_of(mass, album) == []
    assert await albums.album_series() == []
    with pytest.raises(MediaNotFoundError):
        await albums.get_collection("album___Mine")


async def test_adding_needs_a_library_album_and_a_series(mass: MusicAssistant) -> None:
    """Adding to an unknown series, or adding an album not in the library, fails."""
    albums = mass.music.albums
    album = await _add_album(mass, "x1", "Lonely")
    await albums.create_album_series("Mine")

    with pytest.raises(MediaNotFoundError):
        await albums.add_album_series_album("album___Unknown", album.item_id)
    with pytest.raises(MediaNotFoundError):
        await albums.add_album_series_album("album___Mine", "999999")


@pytest.mark.parametrize(
    "command",
    [
        "get_collection",
        "album_series",
        "create_album_series",
        "set_album_series_type",
        "add_album_series_album",
    ],
)
async def test_the_api_parses_the_series_arguments(mass: MusicAssistant, command: str) -> None:
    """Every argument of the series commands goes through the parser of the API."""
    handler = getattr(mass.music.albums, command)
    signature = inspect.signature(handler)
    args = {
        "item_id": "album___Saga",
        "album_id": "1",
        "name": "Saga",
        "series_type": "ranked_list",
        "sequence": "3",
        "album_visibility": "hidden",
    }

    parsed = parse_arguments(
        signature,
        _get_type_hints_for_api_command(handler),
        {key: value for key, value in args.items() if key in signature.parameters},
    )

    if "series_type" in parsed:
        assert parsed["series_type"] == AlbumSeriesType.RANKED_LIST


async def test_an_unknown_album_visibility_is_refused(mass: MusicAssistant) -> None:
    """The album visibility of a series is one of visible, hidden or all."""
    await mass.music.albums.create_album_series("Mine")

    with pytest.raises(InvalidDataError):
        await mass.music.albums.get_collection("album___Mine", album_visibility="gone")
