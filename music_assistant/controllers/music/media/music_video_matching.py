"""Links the library music videos to the library tracks of the same recording (fork)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import AlbumType, ExternalID

from music_assistant.constants import DB_TABLE_TRACK_ARTISTS
from music_assistant.controllers.music.custom_migrations import (
    DB_TABLE_MUSIC_VIDEO_ARTISTS,
    DB_TABLE_MUSIC_VIDEO_TRACKS,
)
from music_assistant.helpers.compare import compare_strings, compare_version
from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from music_assistant_models.media_items import MusicVideo, Track

    from music_assistant import MusicAssistant


class MusicVideoTrackLink(StrEnum):
    """How a music video was linked to a library track."""

    ISRC = "isrc"
    MB_RECORDING = "mb_recording"
    NAME = "name"
    MANUAL = "manual"
    # a link the user removed, kept so the automatic matching does not add it back
    EXCLUDED = "excluded"


# a name match also needs the durations this close, when there is more than one candidate
DURATION_TOLERANCE: Final[int] = 10
# the words of a version that describe the video, not the recording ("Official Video")
_VIDEO_VERSION_WORDS: Final[re.Pattern[str]] = re.compile(
    r"\b(official|music|video|videoclip|clip|visuali[sz]er|hd|4k|uhd|hq|remastered in 4k)\b",
    re.IGNORECASE,
)
# a bracketed part of a name that is about the video, e.g. "[Clip officiel HD]"
_VIDEO_BRACKETS: Final[re.Pattern[str]] = re.compile(
    r"\s*[(\[][^)\]]*\b(video|vid[eé]o|videoclip|clip|visuali[sz]er)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[\s\-/,|]+")


def recording_title(name: str, version: str = "") -> tuple[str, str]:
    """
    Return the title and the version of the recording a music video shows.

    The words that only describe the video ("Official Music Video", "Clip", "4K") are left
    out of the version, so a music video compares with the track of its recording.

    :param name: The name of the music video or track.
    :param version: Its version, if the provider gives one apart from the name.
    """
    title, parsed_version = parse_title_and_version(
        _VIDEO_BRACKETS.sub("", name).strip() or name, version or None
    )
    recording_version = _SEPARATORS.sub(" ", _VIDEO_VERSION_WORDS.sub(" ", parsed_version))
    return title, recording_version.strip()


def same_recording(video: MusicVideo, track: Track) -> bool:
    """
    Return whether a track has the title and the version of the recording of a music video.

    :param video: The music video.
    :param track: The track.
    """
    video_title, video_version = recording_title(video.name, video.version)
    track_title, track_version = recording_title(track.name, track.version)
    return compare_strings(video_title, track_title, strict=False) and compare_version(
        video_version, track_version
    )


def pick_track(
    video: MusicVideo, candidates: list[Track], album_types: dict[str, AlbumType] | None = None
) -> Track | None:
    """
    Return the one track of the candidates the music video shows, or None when unsure.

    :param video: The music video.
    :param candidates: The library tracks of its artists with the same title and version.
    :param album_types: The album type of each candidate track by track id, when known.
    """
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    if video.duration:
        close = [
            track
            for track in candidates
            if track.duration and abs(track.duration - video.duration) <= DURATION_TOLERANCE
        ]
        if len(close) == 1:
            return close[0]
        candidates = close or candidates
    on_albums = [
        track for track in candidates if (album_types or {}).get(track.item_id) == AlbumType.ALBUM
    ]
    return on_albums[0] if len(on_albums) == 1 else None


class MusicVideoMatcher:
    """Finds the library track of each library music video."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the matcher.

        :param mass: The Music Assistant instance.
        """
        self.mass = mass

    async def match_music_video(self, video: MusicVideo) -> list[str]:
        """
        Link a library music video to the library tracks of its recording.

        The external ids decide first (ISRC, then the MusicBrainz recording); without them a
        track of one of its artists with the same title and version is linked, when only one
        fits. A link the user made or removed is kept.

        :param video: The library music video.
        :return: The library ids of the tracks linked by this call.
        """
        tracks = self.mass.music.tracks
        for id_type, source in (
            (ExternalID.ISRC, MusicVideoTrackLink.ISRC),
            (ExternalID.MB_RECORDING, MusicVideoTrackLink.MB_RECORDING),
        ):
            found = [
                track
                for value in {v for t, v in video.external_ids if t == id_type}
                for track in await tracks.get_library_items_by_external_id(
                    value, id_type, limit=None
                )
            ]
            if found:
                return await self._link(video, found, source)
        candidates = await self._name_candidates(video)
        album_types = await self._album_types(candidates) if len(candidates) > 1 else {}
        if track := pick_track(video, candidates, album_types):
            return await self._link(video, [track], MusicVideoTrackLink.NAME)
        return []

    async def match_track(self, track: Track) -> list[str]:
        """
        Link a new library track to the unlinked library music videos of its artists.

        :param track: The library track.
        :return: The library ids of the music videos linked to it.
        """
        artist_ids = [
            int(artist.item_id) for artist in track.artists if artist.provider == "library"
        ]
        if not artist_ids:
            return []
        linked: list[str] = []
        for video in await self._unlinked_videos(artist_ids):
            if str(track.item_id) in await self.match_music_video(video):
                linked.append(video.item_id)
        return linked

    async def match_unlinked(self) -> int:
        """
        Try to link every library music video that has no track yet.

        :return: The number of music videos that got a track.
        """
        count = 0
        for video in await self._unlinked_videos(None):
            if await self.match_music_video(video):
                count += 1
        return count

    async def _link(
        self, video: MusicVideo, found: list[Track], source: MusicVideoTrackLink
    ) -> list[str]:
        """Store the links of a music video to tracks, and return the ids that were stored."""
        return [
            track.item_id
            for track in found
            if await self.mass.music.music_videos.link_track(video.item_id, track.item_id, source)
        ]

    async def _album_types(self, tracks: list[Track]) -> dict[str, AlbumType]:
        """Return the type of the album of each track that is on a library album."""
        album_types: dict[str, AlbumType] = {}
        for track in tracks:
            if track.album is None or track.album.provider != "library":
                continue
            album = await self.mass.music.albums.get_library_item(track.album.item_id)
            album_types[track.item_id] = album.album_type
        return album_types

    async def _name_candidates(self, video: MusicVideo) -> list[Track]:
        """Return the library tracks of the artists of a music video with its title and version."""
        artist_ids = [
            int(artist.item_id) for artist in video.artists if artist.provider == "library"
        ]
        if not artist_ids:
            return []
        placeholders = ", ".join(f":artist_{index}" for index in range(len(artist_ids)))
        artist_tracks = await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[
                f"tracks.item_id IN (SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} "
                f"WHERE artist_id IN ({placeholders}))"
            ],
            extra_query_params={f"artist_{i}": artist_id for i, artist_id in enumerate(artist_ids)},
            limit=0,
        )
        return [track for track in artist_tracks if same_recording(video, track)]

    async def _unlinked_videos(self, artist_ids: list[int] | None) -> list[MusicVideo]:
        """Return the library music videos without a linked track, of these artists when given."""
        query_parts = [
            "music_videos.item_id NOT IN (SELECT music_video_id "
            f"FROM {DB_TABLE_MUSIC_VIDEO_TRACKS} WHERE match_source != 'excluded')"
        ]
        params: dict[str, int] = {}
        if artist_ids:
            placeholders = ", ".join(f":artist_{index}" for index in range(len(artist_ids)))
            query_parts.append(
                f"music_videos.item_id IN (SELECT music_video_id FROM "
                f"{DB_TABLE_MUSIC_VIDEO_ARTISTS} WHERE artist_id IN ({placeholders}))"
            )
            params = {f"artist_{i}": artist_id for i, artist_id in enumerate(artist_ids)}
        return await self.mass.music.music_videos.get_library_items_by_query(
            extra_query_parts=query_parts, extra_query_params=params, limit=0
        )
