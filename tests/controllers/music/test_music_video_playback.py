"""Tests for the video of the music videos, served to the clients (fork)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import (
    AudioFormat,
    MusicVideo,
    ProviderMapping,
    VideoMetadata,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.music.media.music_video_playback import (
    MUSIC_VIDEO_ROUTE_PREFIX,
    MusicVideoPlayback,
    MusicVideoSourceKind,
)
from music_assistant.models.music_provider import MusicProvider

BASE_URL = "http://ma.local:8095"


def _music_video(provider: str = "library", web_playable: bool = True) -> MusicVideo:
    """Return a music video served by the filesystem provider."""
    return MusicVideo(
        item_id="1" if provider == "library" else "Clips/clip.mp4",
        provider=provider,
        name="Clip",
        provider_mappings={
            ProviderMapping(
                item_id="Clips/clip.mp4",
                provider_domain="filesystem_local",
                provider_instance="filesystem_local--1",
            )
        },
        video_metadata=VideoMetadata(video_codec="h264", web_playable=web_playable),
    )


def _playback(path: str, stream_type: StreamType = StreamType.LOCAL_FILE) -> MusicVideoPlayback:
    """Return the playback of a server whose provider streams the given path."""
    provider = MagicMock(spec=MusicProvider)
    provider.get_stream_details = AsyncMock(
        return_value=StreamDetails(
            provider="filesystem_local--1",
            item_id="Clips/clip.mp4",
            audio_format=AudioFormat(),
            media_type=MediaType.MUSIC_VIDEO,
            stream_type=stream_type,
            path=path,
        )
    )
    mass = MagicMock()
    mass.get_provider = MagicMock(return_value=provider)
    mass.webserver.base_url = BASE_URL
    return MusicVideoPlayback(mass)


async def _client(playback: MusicVideoPlayback) -> TestClient[web.Request, web.Application]:
    """Return a test client of an app serving the music video route."""
    app = web.Application()
    app.router.add_get(f"{MUSIC_VIDEO_ROUTE_PREFIX}{{token}}", playback.handle_request)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_an_mp4_file_is_served_as_it_is(tmp_path: Path) -> None:
    """An MP4 file gets a URL of its own, which supports range requests for seeking."""
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"0123456789")
    playback = _playback(str(video_file))

    info = await playback.video_url(_music_video())

    assert info.kind == MusicVideoSourceKind.FILE
    assert info.seekable
    assert info.url.startswith(f"{BASE_URL}{MUSIC_VIDEO_ROUTE_PREFIX}")
    playback.mass.webserver.register_dynamic_route.assert_called_once()
    client = await _client(playback)
    try:
        path = info.url.removeprefix(BASE_URL)
        response = await client.get(path, headers={"Range": "bytes=2-4"})
        assert response.status == 206
        assert await response.read() == b"234"
        assert response.headers["Content-Type"] == "video/mp4"
        assert (await client.get(f"{MUSIC_VIDEO_ROUTE_PREFIX}unknown")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_expired_url_no_longer_works(tmp_path: Path) -> None:
    """A URL past its lifetime is refused."""
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"x")
    playback = _playback(str(video_file))
    info = await playback.video_url(_music_video())
    for served in playback._served.values():
        served.expires_at = 0
    client = await _client(playback)
    try:
        assert (await client.get(info.url.removeprefix(BASE_URL))).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_music_service_hls_playlist_is_given_to_the_client() -> None:
    """The client reads the HLS playlist of a music service itself (fork exception)."""
    playlist = "https://cdn.example.com/video/master.m3u8"
    playback = _playback(playlist, StreamType.HLS)

    info = await playback.video_url(_music_video(web_playable=False))

    assert info.kind == MusicVideoSourceKind.HLS
    assert info.url == playlist
    assert info.expires_at > 0
    assert not info.web_playable
    playback.mass.webserver.register_dynamic_route.assert_not_called()


@pytest.mark.asyncio
async def test_a_stream_without_a_path_can_not_be_shown() -> None:
    """A custom stream has no video a client can read."""
    playback = _playback("", StreamType.CUSTOM)

    with pytest.raises(UnsupportedFeaturedException):
        await playback.video_url(_music_video())


@pytest.mark.asyncio
async def test_the_provider_copy_is_asked_for_a_library_item(tmp_path: Path) -> None:
    """A library music video is read from the provider mapping that serves it."""
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"x")
    playback = _playback(str(video_file))

    await playback.video_url(_music_video())

    playback.mass.get_provider.assert_called_with("filesystem_local--1")
    provider = playback.mass.get_provider.return_value
    provider.get_stream_details.assert_awaited_once_with("Clips/clip.mp4", MediaType.MUSIC_VIDEO)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_an_mkv_file_is_remuxed_without_its_audio(tmp_path: Path) -> None:
    """An MKV file is streamed as fragmented MP4 from the asked position, video only."""
    video_file = tmp_path / "clip.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=4:size=160x120:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=duration=4",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        str(video_file),
    )
    assert await proc.wait() == 0
    playback = _playback(str(video_file))

    info = await playback.video_url(_music_video())

    assert info.kind == MusicVideoSourceKind.REMUX
    assert not info.seekable
    client = await _client(playback)
    try:
        response = await client.get(f"{info.url.removeprefix(BASE_URL)}?start=1")
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp4"
        body = await response.read()
        assert body[4:8] == b"ftyp"
        assert b"soun" not in body
        assert (await client.get(f"{info.url.removeprefix(BASE_URL)}?start=x")).status == 400
    finally:
        await client.close()
