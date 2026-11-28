"""Tests for the music videos of the fork: library storage, artist and track links, stream."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID, ListingType, MediaType, StreamType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    MusicVideo,
    MusicVideoSummary,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.music.custom_migrations import (
    DB_TABLE_MUSIC_VIDEO_ARTISTS,
    DB_TABLE_MUSIC_VIDEO_TRACKS,
)
from music_assistant.controllers.music.media.music_videos import MusicVideoTrackLink
from music_assistant.controllers.music.sorting import LIBRARY_LISTINGS
from music_assistant.helpers.music_videos import drop_video_streams

from .helpers import create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


ISRC = "GBUM71029604"


def _create_music_video(
    item_id: str, name: str = "Clip", artist_name: str = "Test Artist"
) -> MusicVideo:
    """Create a MusicVideo as it would be received from a music provider."""
    artist_id = f"{artist_name}_artist"
    return MusicVideo(
        item_id=item_id,
        provider="tidal_1",
        name=name,
        duration=215,
        external_ids={(ExternalID.ISRC, ISRC)},
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="tidal",
                provider_instance="tidal_1",
                audio_format=AudioFormat(),
                in_library=True,
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id=artist_id,
                    provider="tidal_1",
                    name=artist_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_id,
                            provider_domain="tidal",
                            provider_instance="tidal_1",
                        )
                    },
                )
            ]
        ),
    )


async def _add_track(mass: MusicAssistant, item_id: str, name: str = "Clip") -> str:
    """Add a track to the library and return its library id."""
    track = create_track("tidal_1", item_id, name=name, isrc=f"TRACK{item_id}")
    for mapping in track.provider_mappings:
        mapping.in_library = True
    return (await mass.music.tracks.add_item_to_library(track)).item_id


async def _link_sources(mass: MusicAssistant, video_id: str) -> dict[int, str]:
    """Return the stored track links of a library music video, by track id."""
    rows = await mass.music.database.get_rows(
        DB_TABLE_MUSIC_VIDEO_TRACKS, {"music_video_id": int(video_id)}, limit=0
    )
    return {row["track_id"]: row["match_source"] for row in rows}


@pytest.mark.asyncio
async def test_a_music_video_is_stored_with_its_artists(music_mass: MusicAssistant) -> None:
    """A music video added to the library comes back with its fields and artists."""
    added = await music_mass.music.music_videos.add_item_to_library(_create_music_video("v1"))

    stored = await music_mass.music.music_videos.get_library_item(added.item_id)

    assert isinstance(stored, MusicVideo)
    assert stored.media_type == MediaType.MUSIC_VIDEO
    assert stored.name == "Clip"
    assert stored.duration == 215
    assert stored.artist_str == "Test Artist"
    assert stored.track is None
    assert (ExternalID.ISRC, ISRC) in stored.external_ids
    assert await music_mass.music.music_videos.library_count() == 1


@pytest.mark.asyncio
async def test_a_music_video_needs_an_artist(music_mass: MusicAssistant) -> None:
    """A music video without artists is refused, like a track."""
    video = _create_music_video("v1")
    video.artists = UniqueList()

    with pytest.raises(InvalidDataError):
        await music_mass.music.music_videos.add_item_to_library(video)


@pytest.mark.asyncio
async def test_the_library_lists_music_videos_as_summaries(music_mass: MusicAssistant) -> None:
    """The library listing returns slim summaries with the artists and the linked track."""
    videos = music_mass.music.music_videos
    added = await videos.add_item_to_library(_create_music_video("v1"))
    track_id = await _add_track(music_mass, "t1")
    await videos.set_track_link(added.item_id, track_id)

    listed = await videos.library_items(summary=True)

    assert len(listed) == 1
    summary = listed[0]
    assert isinstance(summary, MusicVideoSummary)
    assert summary.duration == 215
    assert [artist.name for artist in summary.artists] == ["Test Artist"]
    assert summary.track is not None
    assert summary.track.item_id == track_id


@pytest.mark.asyncio
async def test_by_artist_lists_the_library_music_videos(music_mass: MusicAssistant) -> None:
    """The music videos of a library artist are the ones it is credited on."""
    videos = music_mass.music.music_videos
    first = await videos.add_item_to_library(_create_music_video("v1", "First"))
    await videos.add_item_to_library(_create_music_video("v2", "Other", artist_name="Someone"))
    artist = first.artists[0]

    listed = await videos.by_artist(artist.item_id, "library", in_library_only=True)

    assert [video.name for video in listed] == ["First"]


@pytest.mark.asyncio
async def test_a_manual_link_replaces_the_automatic_ones(music_mass: MusicAssistant) -> None:
    """Linking by hand drops the automatic links, and the track lists its music video."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1"))
    wrong_id = await _add_track(music_mass, "t1", name="Wrong")
    right_id = await _add_track(music_mass, "t2", name="Right")
    assert await videos.link_track(video.item_id, wrong_id, MusicVideoTrackLink.NAME)

    linked = await videos.set_track_link(video.item_id, right_id)

    assert linked.track is not None
    assert linked.track.item_id == right_id
    assert await _link_sources(music_mass, video.item_id) == {int(right_id): "manual"}
    assert [v.item_id for v in await videos.for_track(right_id, "library")] == [video.item_id]
    assert await videos.for_track(wrong_id, "library") == []


@pytest.mark.asyncio
async def test_a_removed_link_does_not_come_back(music_mass: MusicAssistant) -> None:
    """A link the user removed stays removed when the automatic matching finds it again."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1"))
    track_id = await _add_track(music_mass, "t1")
    await videos.link_track(video.item_id, track_id, MusicVideoTrackLink.ISRC)

    unlinked = await videos.remove_track_link(video.item_id, track_id)

    assert unlinked.track is None
    assert not await videos.link_track(video.item_id, track_id, MusicVideoTrackLink.ISRC)
    assert await _link_sources(music_mass, video.item_id) == {int(track_id): "excluded"}
    assert await videos.for_track(track_id, "library") == []


@pytest.mark.asyncio
async def test_removing_a_music_video_drops_its_links(
    music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A music video removed from the library leaves no artist or track link behind."""
    # remove_item_from_library routes audio analysis cleanup through the streams controller
    streams = MagicMock()
    streams.audio_analysis.delete_audio_analysis = AsyncMock()
    monkeypatch.setattr(music_mass, "streams", streams, raising=False)
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1"))
    track_id = await _add_track(music_mass, "t1")
    await videos.set_track_link(video.item_id, track_id)

    await videos.remove_item_from_library(video.item_id)

    for table in (DB_TABLE_MUSIC_VIDEO_ARTISTS, DB_TABLE_MUSIC_VIDEO_TRACKS):
        assert not await music_mass.music.database.get_rows(
            table, {"music_video_id": int(video.item_id)}, limit=0
        )


@pytest.mark.asyncio
async def test_the_music_video_controller_is_registered(music_mass: MusicAssistant) -> None:
    """The music controller and the sort options know the music videos."""
    assert music_mass.music.get_controller(MediaType.MUSIC_VIDEO) is music_mass.music.music_videos
    assert LIBRARY_LISTINGS[MediaType.MUSIC_VIDEO] == ListingType.LIBRARY_MUSIC_VIDEOS


def _streamdetails(
    media_type: MediaType, extra_input_args: list[str] | None = None
) -> StreamDetails:
    return StreamDetails(
        provider="tidal_1",
        item_id="1",
        audio_format=AudioFormat(),
        media_type=media_type,
        stream_type=StreamType.HTTP,
        path="https://example.com/video.m3u8",
        extra_input_args=extra_input_args or [],
    )


def test_the_stream_of_a_music_video_drops_the_video() -> None:
    """Ffmpeg reads only the audio of a music video, once, after the provider's own args."""
    queue_item = QueueItem.from_media_item("queue1", _create_music_video("v1"))
    details = _streamdetails(MediaType.MUSIC_VIDEO, ["-headers", "x"])

    drop_video_streams(queue_item, details)
    drop_video_streams(queue_item, details)

    assert details.extra_input_args == ["-headers", "x", "-vn"]


def test_the_stream_of_a_track_keeps_its_streams() -> None:
    """The stream of a track is left as the provider made it."""
    queue_item = QueueItem.from_media_item("queue1", create_track("tidal_1", "t1"))
    details = _streamdetails(MediaType.TRACK)

    drop_video_streams(queue_item, details)

    assert details.extra_input_args == []
