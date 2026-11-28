"""Tests for the music videos of the Tidal provider (fork)."""

from __future__ import annotations

import base64
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import MusicVideo, Track

from music_assistant.providers.tidal.media import TidalMediaManager
from music_assistant.providers.tidal.music_videos import (
    ARTIST_MUSIC_VIDEOS_LIMIT,
    TidalMusicVideoManager,
    hls_url_from_playback_info,
    parse_music_video,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
HLS_URL = "https://im-fa.manifest.tidal.com/1/manifests/master.m3u8?token=abc"


def _video_fixture() -> dict[str, Any]:
    with open(FIXTURES_DIR / "videos" / "video.json", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _track_fixture() -> dict[str, Any]:
    with open(FIXTURES_DIR / "tracks" / "track.json", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _emu_playback_info() -> dict[str, Any]:
    manifest = {"mimeType": "application/vnd.apple.mpegurl", "urls": [HLS_URL]}
    return {
        "videoId": 55443322,
        "streamType": "ON_DEMAND",
        "assetPresentation": "FULL",
        "videoQuality": "HIGH",
        "manifestMimeType": "application/vnd.tidal.emu",
        "manifest": base64.b64encode(json.dumps(manifest).encode()).decode(),
    }


def test_a_tidal_video_is_parsed_with_its_artists_and_thumbnail(provider_mock: Mock) -> None:
    """Every artist, the album, the 3:2 thumbnail and the dates are read."""
    video = parse_music_video(provider_mock, _video_fixture())

    assert video.item_id == "55443322"
    assert video.media_type == MediaType.MUSIC_VIDEO
    assert video.name == "Test Video"
    assert video.duration == 245
    assert [artist.name for artist in video.artists] == ["Test Artist", "Guest Artist"]
    assert video.album is not None
    assert video.album.item_id == "998877"
    assert video.image is not None
    assert video.image.path.endswith("/aa11bb22/cc33/dd44/ee55/ff6677889900/1080x720.jpg")
    assert video.metadata.release_date is not None
    assert video.metadata.release_date.year == 2011
    assert video.date_added is not None
    assert video.video_metadata is not None
    assert video.video_metadata.web_playable
    mapping = next(iter(video.provider_mappings))
    assert mapping.url == "https://tidal.com/video/55443322"


def test_the_hls_playlist_comes_from_the_emu_manifest() -> None:
    """The playback info of a video holds the HLS playlist in a base64 JSON manifest."""
    assert hls_url_from_playback_info(_emu_playback_info()) == HLS_URL
    with pytest.raises(MediaNotFoundError):
        hls_url_from_playback_info({"manifestMimeType": "application/vnd.tidal.emu"})


@pytest.mark.asyncio
async def test_the_stream_of_a_video_is_its_hls_playlist(provider_mock: Mock) -> None:
    """The players get the audio of the HLS playlist; the client the same playlist."""
    provider_mock.get_music_video = AsyncMock(
        return_value=parse_music_video(provider_mock, _video_fixture())
    )
    provider_mock.api.get = AsyncMock(return_value=_emu_playback_info())
    manager = TidalMusicVideoManager(provider_mock)

    details = await manager.get_stream_details("55443322")

    assert details.media_type == MediaType.MUSIC_VIDEO
    assert details.stream_type == StreamType.HLS
    assert details.path == HLS_URL
    assert details.audio_format.content_type == ContentType.AAC
    assert details.duration == 245
    provider_mock.api.get.assert_awaited_once_with(
        "videos/55443322/playbackinfopostpaywall",
        params={"videoquality": "HIGH", "playbackmode": "STREAM", "assetpresentation": "FULL"},
    )


@pytest.mark.asyncio
async def test_the_favorite_videos_are_paged(provider_mock: Mock) -> None:
    """The library walks the favorite videos until the total is reached."""
    video = _video_fixture()
    provider_mock.api.get = AsyncMock(
        side_effect=[
            {"items": [video, video], "totalNumberOfItems": 3},
            {"items": [video], "totalNumberOfItems": 3},
        ]
    )
    manager = TidalMusicVideoManager(provider_mock)

    videos = [item async for item in manager.library()]

    assert len(videos) == 3
    first_call = provider_mock.api.get.await_args_list[0]
    assert first_call.args == ("users/12345/favorites/videos",)
    assert first_call.kwargs["params"]["offset"] == 0
    assert provider_mock.api.get.await_args_list[1].kwargs["params"]["offset"] == 2


@pytest.mark.asyncio
async def test_a_broken_favorite_is_skipped(provider_mock: Mock) -> None:
    """A favorite that does not parse is reported, the others still come."""
    provider_mock.api.get = AsyncMock(
        return_value={
            "items": [{"item": {"title": "no id"}}, _video_fixture()],
            "totalNumberOfItems": 2,
        }
    )
    manager = TidalMusicVideoManager(provider_mock)

    videos = [item async for item in manager.library()]

    assert [video.item_id for video in videos] == ["55443322"]
    provider_mock.report_skipped_sync_item.assert_called_once()


@pytest.mark.asyncio
async def test_favorites_are_added_and_removed(provider_mock: Mock) -> None:
    """A favorite video is written through the v1 favorites of the user."""
    manager = TidalMusicVideoManager(provider_mock)

    assert await manager.add_favorite("55443322")
    assert await manager.remove_favorite("55443322")

    provider_mock.api.post.assert_awaited_once_with(
        "users/12345/favorites/videos", data={"videoIds": "55443322"}, as_form=True
    )
    provider_mock.api.delete.assert_awaited_once_with("users/12345/favorites/videos/55443322")


@pytest.mark.asyncio
async def test_artist_videos_and_search(provider_mock: Mock) -> None:
    """The videos of an artist are capped, and a search reads the videos section."""
    item = _video_fixture()["item"]
    provider_mock.api.get = AsyncMock(
        side_effect=[{"items": [item]}, {"videos": {"items": [item, item]}}]
    )
    manager = TidalMusicVideoManager(provider_mock)

    by_artist = await manager.artist_videos("12345")
    found = await manager.search("test", limit=1)

    assert [video.item_id for video in by_artist] == ["55443322"]
    assert len(found) == 1
    calls = provider_mock.api.get.await_args_list
    assert calls[0].args == ("artists/12345/videos",)
    assert calls[0].kwargs["params"] == {"limit": ARTIST_MUSIC_VIDEOS_LIMIT}
    assert calls[1].kwargs["params"]["types"] == "VIDEOS"


@pytest.mark.asyncio
async def test_a_playlist_mixes_tracks_and_videos(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """The items of a playlist keep their order and their own type."""
    track_item = {**_track_fixture(), "type": "track"}
    video_item = {**_video_fixture(), "type": "video"}
    provider_mock.api.get.return_value = {"items": [track_item, video_item]}

    items = await media_manager.get_playlist_tracks("1")

    assert [type(item) for item in items] == [Track, MusicVideo]
    assert [item.position for item in items] == [1, 2]  # type: ignore[union-attr]
