"""Tests for the music videos of the local filesystem provider (fork)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature, StreamType
from music_assistant_models.media_items import Artist, MusicVideo

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CONF_ENTRY_CONTENT_TYPE
from music_assistant.providers.filesystem_local.helpers import FileSystemItem
from music_assistant.providers.filesystem_local.music_videos import (
    CONF_MUSIC_VIDEOS_FOLDER,
    FilesystemMusicVideos,
    in_music_videos_folder,
    music_videos_folder,
    names_from_filename,
    parse_music_video_nfo,
    video_metadata_from_probe,
)

INSTANCE_ID = "filesystem_local--test"

NFO = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<musicvideo>
    <title>Karma Police</title>
    <artist>Radiohead</artist>
    <artist>Guest</artist>
    <album>OK Computer</album>
    <premiered>1997-08-25</premiered>
    <genre>Rock</genre>
    <plot>The clip.</plot>
    <musicBrainzArtistID>a74b1b7f-71a5-4011-9441-d0b5e4122711</musicBrainzArtistID>
    <musicbrainztrackid>9e2b1a1c-0b4f-4b6b-9a7c-2a1f0e3b5c6d</musicbrainztrackid>
</musicvideo>
"""


def _probe(codec: str = "h264", attached_pic: int = 0) -> dict[str, Any]:
    """Return a ffprobe output with one video and one audio stream."""
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "filename": "x.mp4"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": 1920,
                "height": 1080,
                "disposition": {"attached_pic": attached_pic},
            },
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
    }


def _create_provider(folder: str | None = "Clips") -> LocalFileSystemProvider:
    """
    Create a music LocalFileSystemProvider with mocked dependencies.

    :param folder: The configured music videos folder.
    """
    config = MagicMock()
    config.get_value = MagicMock(
        side_effect=lambda key: {
            CONF_ENTRY_CONTENT_TYPE.key: "music",
            CONF_MUSIC_VIDEOS_FOLDER: folder,
        }.get(key)
    )
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = config
    provider.manifest = MagicMock()
    provider.manifest.domain = "filesystem_local"
    provider.media_content_type = "music"
    provider.write_access = False
    provider.base_path = "/media"
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    return provider


def _file(relative_path: str) -> FileSystemItem:
    """Return a file of the provider."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum="1",
        file_size=1000,
    )


def _tags(title: str | None = None, artist: str | None = None) -> MagicMock:
    """Return mocked AudioTags of a video file."""
    tags = MagicMock()
    tags.raw = _probe()
    tags.tags = {key: value for key, value in (("title", title), ("artist", artist)) if value}
    tags.duration = 263.5
    tags.sample_rate = 48000
    tags.bits_per_sample = 16
    tags.channels = 2
    tags.bit_rate = 2000
    tags.format = "mov,mp4"
    return tags


def _mock_reader(
    provider: LocalFileSystemProvider, tags: MagicMock, nfo: bytes | None
) -> FilesystemMusicVideos:
    """Return the music video reader of a provider whose file access is mocked."""
    provider._parse_tags = AsyncMock(return_value=tags)  # type: ignore[method-assign]
    provider.exists = AsyncMock(return_value=nfo is not None)  # type: ignore[method-assign]
    provider._read_file = AsyncMock(return_value=nfo or b"")  # type: ignore[method-assign]
    provider._scandir = AsyncMock(  # type: ignore[method-assign]
        return_value=[_file("Clips/Radiohead - Karma Police-thumb.jpg")]
    )

    async def _parse_artist(name: str, mbid: str | None = None, **_kw: Any) -> Artist:
        artist = Artist(item_id=name, provider=INSTANCE_ID, name=name, provider_mappings=set())
        if mbid:
            artist.mbid = mbid
        return artist

    provider._parse_artist = _parse_artist  # type: ignore[method-assign]
    return provider._music_videos


def test_the_folder_setting_is_a_relative_path() -> None:
    """The setting is read without surrounding slashes, and anything but text means none."""
    assert music_videos_folder(" /Clips/Live\\ ") == "Clips/Live"
    assert music_videos_folder("") == ""
    assert music_videos_folder(None) == ""


def test_only_paths_inside_the_folder_belong_to_it() -> None:
    """A path belongs to the folder when it is the folder or lies under it."""
    assert in_music_videos_folder("Clips/a.mp4", "Clips")
    assert in_music_videos_folder("Clips", "Clips")
    assert not in_music_videos_folder("Clips Live/a.mp4", "Clips")
    assert not in_music_videos_folder("Clips/a.mp4", "")


def test_a_file_name_gives_the_artist_and_the_title() -> None:
    """A name without the separator is the title alone."""
    assert names_from_filename("Radiohead - Karma Police") == ("Radiohead", "Karma Police")
    assert names_from_filename("Karma Police") == (None, "Karma Police")
    assert names_from_filename(" - Karma Police") == (None, "- Karma Police")


def test_a_kodi_nfo_gives_the_music_video_fields() -> None:
    """Every artist, the first release year and the MusicBrainz ids are read."""
    nfo = parse_music_video_nfo(NFO)

    assert nfo is not None
    assert nfo.title == "Karma Police"
    assert nfo.artists == ["Radiohead", "Guest"]
    assert nfo.album == "OK Computer"
    assert nfo.year == 1997
    assert nfo.genres == {"Rock"}
    assert nfo.mb_artist_ids == ["a74b1b7f-71a5-4011-9441-d0b5e4122711"]
    assert nfo.mb_recording_id == "9e2b1a1c-0b4f-4b6b-9a7c-2a1f0e3b5c6d"


def test_an_nfo_of_another_kind_is_ignored() -> None:
    """An album NFO or a broken file is not a music video NFO."""
    assert parse_music_video_nfo(b"<album><title>x</title></album>") is None
    assert parse_music_video_nfo(b"not xml") is None


def test_the_video_stream_tells_whether_a_browser_plays_it() -> None:
    """H.264 plays in a browser, HEVC does not, and a cover image is no video."""
    h264 = video_metadata_from_probe(_probe("h264"))
    assert h264 is not None
    assert (h264.width, h264.height, h264.web_playable) == (1920, 1080, True)
    hevc = video_metadata_from_probe(_probe("hevc"))
    assert hevc is not None
    assert not hevc.web_playable
    assert video_metadata_from_probe(_probe("mjpeg", attached_pic=1)) is None


def test_the_folder_adds_the_music_video_library() -> None:
    """The provider offers a music video library only with a music videos folder."""
    assert ProviderFeature.LIBRARY_MUSIC_VIDEOS in _create_provider("Clips").supported_features
    assert ProviderFeature.LIBRARY_MUSIC_VIDEOS not in _create_provider(None).supported_features


def test_the_audio_scan_skips_the_music_videos_folder() -> None:
    """A file of the folder is never queued as a track, nor recorded as present."""
    provider = _create_provider()
    items: list[tuple[FileSystemItem, str | None]] = []
    present: set[str] = set()

    for path in ("Clips/Radiohead - Karma Police.mp4", "Album/01.mp4"):
        provider._classify_scan_item(
            _file(path),
            file_checksums={},
            cue_file_checksums={},
            cur_filenames=present,
            items_to_process=items,
            unchanged_cue_items=[],
            cue_stems=set(),
            ignore_album_playlists=False,
            metadata_files=[],
        )

    assert [item.relative_path for item, _ in items] == ["Album/01.mp4"]
    assert present == set()


@pytest.mark.asyncio
async def test_a_video_with_an_nfo_takes_its_fields() -> None:
    """The NFO names the music video, its artists and recording; the thumbnail is found."""
    provider = _create_provider()
    reader = _mock_reader(provider, _tags(title="Tag title"), NFO)

    video = await reader.parse(_file("Clips/Radiohead - Karma Police.mp4"))

    assert isinstance(video, MusicVideo)
    assert video.name == "Karma Police"
    assert video.duration == 263
    assert [artist.name for artist in video.artists] == ["Radiohead", "Guest"]
    assert video.artists[0].mbid == "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    assert video.album is not None
    assert video.album.name == "OK Computer"
    assert video.metadata.release_date is not None
    assert video.metadata.release_date.year == 1997
    assert (ExternalID.MB_RECORDING, "9e2b1a1c-0b4f-4b6b-9a7c-2a1f0e3b5c6d") in video.external_ids
    assert video.video_metadata is not None
    assert video.video_metadata.web_playable
    assert video.image is not None
    assert video.image.path == "Clips/Radiohead - Karma Police-thumb.jpg"


@pytest.mark.asyncio
async def test_a_video_without_nfo_is_named_by_its_file() -> None:
    """Without NFO or tags, the file name gives the artist and the title."""
    provider = _create_provider()
    reader = _mock_reader(provider, _tags(), None)

    video = await reader.parse(_file("Clips/Radiohead - Karma Police.mp4"))

    assert video.name == "Karma Police"
    assert video.artist_str == "Radiohead"


@pytest.mark.asyncio
async def test_a_file_outside_the_folder_is_no_music_video() -> None:
    """Only a video file of the folder can be fetched as a music video."""
    reader = _mock_reader(_create_provider(), _tags(), None)

    with pytest.raises(Exception, match="Music video not found"):
        await reader.get("Album/01.mp4")


@pytest.mark.asyncio
async def test_the_stream_of_a_music_video_is_its_file() -> None:
    """The players get the audio of the video file, which can be sought."""
    provider = _create_provider()
    reader = _mock_reader(provider, _tags(), None)
    provider.mass.music.music_videos.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.resolve = AsyncMock(  # type: ignore[method-assign]
        return_value=_file("Clips/Radiohead - Karma Police.mp4")
    )

    details = await reader.get_stream_details("Clips/Radiohead - Karma Police.mp4")

    assert details.media_type == MediaType.MUSIC_VIDEO
    assert details.stream_type == StreamType.LOCAL_FILE
    assert details.path == "/media/Clips/Radiohead - Karma Police.mp4"
    assert details.allow_seek
