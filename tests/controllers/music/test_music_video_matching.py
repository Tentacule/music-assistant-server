"""Tests for the linking of the music videos to the tracks of their recording (fork)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import AlbumType, ExternalID
from music_assistant_models.media_items import MusicVideo, Track

from music_assistant.controllers.music.media.music_video_matching import (
    MusicVideoTrackLink,
    pick_track,
    recording_title,
    same_recording,
)

from .helpers import create_track
from .test_music_videos import _create_music_video, _link_sources

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _video(name: str, duration: int = 215) -> MusicVideo:
    return MusicVideo(
        item_id="v", provider="library", name=name, duration=duration, provider_mappings=set()
    )


def _track(item_id: str, name: str, duration: int = 210) -> Track:
    return Track(
        item_id=item_id, provider="library", name=name, duration=duration, provider_mappings=set()
    )


def test_the_video_words_are_not_part_of_the_recording() -> None:
    """'Official Video', 'Clip' or '4K' describe the video, 'Live' the recording."""
    assert recording_title("Karma Police (Official Music Video)") == ("Karma Police", "")
    assert recording_title("Karma Police [Clip officiel HD]") == ("Karma Police", "")
    assert recording_title("Karma Police (Live)")[1].lower() == "live"


def test_a_live_video_is_not_the_studio_recording() -> None:
    """The title and the version of the recording must both match."""
    assert same_recording(_video("Karma Police (Official Video)"), _track("1", "Karma Police"))
    assert not same_recording(_video("Karma Police (Live)"), _track("1", "Karma Police"))
    assert same_recording(_video("Karma Police (Live)"), _track("1", "Karma Police (Live)"))
    assert not same_recording(_video("No Surprises"), _track("1", "Karma Police"))


def test_one_candidate_is_picked_only_when_it_is_the_only_fit() -> None:
    """The duration, then a studio album, decide between candidates; else there is no link."""
    video = _video("Song", duration=215)
    near = _track("1", "Song", duration=210)
    far = _track("2", "Song", duration=400)
    assert pick_track(video, [near]) is near
    assert pick_track(video, [near, far]) is near
    twin = _track("3", "Song", duration=212)
    assert pick_track(video, [near, twin]) is None
    assert pick_track(video, [near, twin], {"1": AlbumType.SINGLE, "3": AlbumType.ALBUM}) is twin
    assert pick_track(video, []) is None


async def _add_track(mass: MusicAssistant, item_id: str, name: str, isrc: str) -> str:
    """Add a track of 'Test Artist' to the library and return its library id."""
    track = create_track("tidal_1", item_id, name=name, isrc=isrc)
    for mapping in track.provider_mappings:
        mapping.in_library = True
    return (await mass.music.tracks.add_item_to_library(track)).item_id


@pytest.mark.asyncio
async def test_the_isrc_links_a_video_to_its_track(music_mass: MusicAssistant) -> None:
    """A track with the ISRC of the music video is its recording, whatever its name."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1", name="Clip"))
    isrc = next(value for id_type, value in video.external_ids if id_type == ExternalID.ISRC)
    track_id = await _add_track(music_mass, "t1", "Another Name", isrc)

    assert await music_mass.music.music_videos.matcher.match_music_video(video) == [track_id]
    assert await _link_sources(music_mass, video.item_id) == {int(track_id): "isrc"}


@pytest.mark.asyncio
async def test_the_name_links_a_video_of_the_same_artist(music_mass: MusicAssistant) -> None:
    """Without a shared id, the track of the artist with the title of the video is linked."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(
        _create_music_video("v1", name="Song (Official Video)")
    )
    track_id = await _add_track(music_mass, "t1", "Song", "TRACK1")
    await _add_track(music_mass, "t2", "Song (Live)", "TRACK2")

    assert await videos.matcher.match_music_video(video) == [track_id]
    assert await _link_sources(music_mass, video.item_id) == {int(track_id): "name"}


@pytest.mark.asyncio
async def test_a_new_track_gets_the_videos_waiting_for_it(music_mass: MusicAssistant) -> None:
    """A track added after its music video is linked to it."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1", name="Song"))
    track_id = await _add_track(music_mass, "t1", "Song", "TRACK1")
    track = await music_mass.music.tracks.get_library_item(track_id)

    assert await videos.matcher.match_track(track) == [video.item_id]
    assert await videos.matcher.match_unlinked() == 0


@pytest.mark.asyncio
async def test_the_matching_keeps_what_the_user_decided(music_mass: MusicAssistant) -> None:
    """A removed link is not added back, and match_tracks reports no new link."""
    videos = music_mass.music.music_videos
    video = await videos.add_item_to_library(_create_music_video("v1", name="Song"))
    track_id = await _add_track(music_mass, "t1", "Song", "TRACK1")
    await videos.remove_track_link(video.item_id, track_id)

    assert await videos.match_tracks(video.item_id) == 0
    assert await _link_sources(music_mass, video.item_id) == {
        int(track_id): MusicVideoTrackLink.EXCLUDED.value
    }
