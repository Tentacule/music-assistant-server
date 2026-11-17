"""Tests for the filesystem provider's album lookup."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--test"
COMPILATION_ID = "Various Artists/Now 1987"
COMPILATION_FILE = "Various Artists/Now (1980-1999)/1987/CD1/04 - Pet Shop Boys - It's A Sin.mp3"
ARTIST_ALBUM_FILE = "Pet Shop Boys/Actually/07 - It's a Sin.mp3"


def _album(item_id: str, name: str) -> Album:
    """Return a provider album with the given id and name."""
    return Album(item_id=item_id, provider=INSTANCE_ID, name=name, provider_mappings=set())


def _mapping(item_id: str) -> ProviderMapping:
    """Return a mapping of a file on the test provider."""
    return ProviderMapping(
        item_id=item_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID
    )


def _create_provider(
    album_by_file: dict[str, Album], tracks: list[Track]
) -> LocalFileSystemProvider:
    """Create a provider whose files parse to the given albums and whose album holds the tracks."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock(instance_id=INSTANCE_ID)

    async def _iter_album_tracks(_prov_album_id: str) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    async def _parse_track(file_item: MagicMock, _tags: object) -> Track:
        return Track(
            item_id=file_item.path,
            provider=INSTANCE_ID,
            name="It's a Sin",
            provider_mappings=set(),
            album=album_by_file[file_item.path],
        )

    provider._album_folder_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
    provider._iter_album_tracks = _iter_album_tracks  # type: ignore[method-assign,assignment]
    provider.resolve = AsyncMock(side_effect=lambda path: MagicMock(path=path))  # type: ignore[method-assign]
    provider._parse_tags = AsyncMock()  # type: ignore[method-assign]
    provider._parse_track = _parse_track  # type: ignore[method-assign,assignment]
    return provider


async def test_get_album_skips_a_file_of_another_album() -> None:
    """A track merged with its file on the artist's own album gives the compilation's tags."""
    compilation = _album(COMPILATION_ID, "Now 1987")
    actually = _album("Pet Shop Boys/Actually", "Actually")
    track = Track(
        item_id="1",
        provider="library",
        name="It's a Sin",
        # the other album's file listed first, as a set may well iterate it
        provider_mappings=[_mapping(ARTIST_ALBUM_FILE), _mapping(COMPILATION_FILE)],
    )
    provider = _create_provider(
        {ARTIST_ALBUM_FILE: actually, COMPILATION_FILE: compilation}, [track]
    )

    assert await provider.get_album(COMPILATION_ID) is compilation


async def test_get_album_falls_back_to_a_file_of_another_album() -> None:
    """With no file parsing to the album's own id, the first parsed album is still returned."""
    retagged = _album("Various Artists/Now 1987 (Remaster)", "Now 1987 (Remaster)")
    track = Track(
        item_id="1",
        provider="library",
        name="It's a Sin",
        provider_mappings={_mapping(COMPILATION_FILE)},
    )
    provider = _create_provider({COMPILATION_FILE: retagged}, [track])

    assert await provider.get_album(COMPILATION_ID) is retagged
