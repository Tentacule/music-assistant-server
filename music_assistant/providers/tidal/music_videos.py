"""Music videos of the Tidal provider (fork)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import ContentType, ExternalID, ImageType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    MusicVideo,
    ProviderMapping,
    UniqueList,
    VideoMetadata,
)
from music_assistant_models.streamdetails import StreamDetails

from .constants import RESOURCES_URL, SKIPPABLE_ITEM_ERRORS
from .parsers import parse_artist

if TYPE_CHECKING:
    from .provider import TidalProvider

# the type Tidal gives a video in a playlist or album item list
TIDAL_VIDEO_ITEM_TYPE: Final[str] = "video"
# Tidal's video thumbnails are 3:2; this is the largest size it serves
_THUMB_SIZE: Final[str] = "1080x720"
# the playback info of a video holds its HLS playlist URL in this (base64 JSON) manifest type
_MANIFEST_EMU: Final[str] = "application/vnd.tidal.emu"
_VIDEO_QUALITY: Final[str] = "HIGH"
_PAGE_SIZE: Final[int] = 100
# an artist's videos are a catalogue listing, so they are capped
ARTIST_MUSIC_VIDEOS_LIMIT: Final[int] = 100
# Tidal serves its music videos as H.264 in HLS, which every client of the fork plays
_VIDEO_CODEC: Final[str] = "h264"


def parse_music_video(provider: TidalProvider, video_obj: dict[str, Any]) -> MusicVideo:
    """
    Parse a Tidal (v1) video object into a MusicVideo.

    :param provider: The Tidal provider the video comes from.
    :param video_obj: The video object, or a favorite/playlist item holding it under "item".
    """
    video_data = video_obj.get("item", video_obj)
    video_id = str(video_data["id"])
    video = MusicVideo(
        item_id=video_id,
        provider=provider.instance_id,
        name=video_data.get("title") or "Unknown",
        version=video_data.get("version") or "",
        duration=int(video_data.get("duration") or 0),
        provider_mappings={
            ProviderMapping(
                item_id=video_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.AAC),
                url=f"https://tidal.com/video/{video_id}",
                available=video_data.get("streamReady", True),
            )
        },
        video_metadata=VideoMetadata(video_codec=_VIDEO_CODEC, container="hls"),
    )
    video.artists = UniqueList()
    for artist_obj in video_data.get("artists") or (
        [video_data["artist"]] if video_data.get("artist") else []
    ):
        try:
            video.artists.append(parse_artist(provider, artist_obj))
        except SKIPPABLE_ITEM_ERRORS as err:
            provider.logger.warning("Error parsing artist of video %s: %s", video.name, err)
    if isrc := video_data.get("isrc"):
        video.external_ids.add((ExternalID.ISRC, isrc))
    if (album_obj := video_data.get("album")) and album_obj.get("id"):
        video.album = provider.get_item_mapping(
            media_type=MediaType.ALBUM,
            key=str(album_obj["id"]),
            name=album_obj.get("title") or "",
        )
    if image_id := video_data.get("imageId"):
        video.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=f"{RESOURCES_URL}/{image_id.replace('-', '/')}/{_THUMB_SIZE}.jpg",
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    video.metadata.explicit = video_data.get("explicit", False)
    video.metadata.popularity = video_data.get("popularity", 0)
    if release_date := video_data.get("releaseDate"):
        with suppress(ValueError):
            video.metadata.release_date = datetime.fromisoformat(release_date)
    if created := video_obj.get("created"):
        with suppress(ValueError):
            video.date_added = datetime.fromisoformat(created).replace(microsecond=0)
    return video


def hls_url_from_playback_info(playback_info: dict[str, Any]) -> str:
    """
    Return the HLS playlist URL in the playback info of a Tidal video.

    :param playback_info: The playbackinfopostpaywall response of a video.
    :raises MediaNotFoundError: When the response holds no playlist URL.
    """
    urls: list[str] = []
    if playback_info.get("manifestMimeType") == _MANIFEST_EMU and "manifest" in playback_info:
        manifest = json.loads(base64.b64decode(playback_info["manifest"]))
        urls = manifest.get("urls") or []
    else:
        urls = playback_info.get("urls") or []
    if not urls:
        raise MediaNotFoundError("No video stream URL found")
    return str(urls[0])


class TidalMusicVideoManager:
    """Reads the music videos of Tidal through its (unofficial) v1 API."""

    def __init__(self, provider: TidalProvider) -> None:
        """
        Initialize the music video manager.

        :param provider: The Tidal provider.
        """
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth

    async def get(self, prov_video_id: str) -> MusicVideo:
        """
        Return a Tidal music video.

        :param prov_video_id: The Tidal id of the video.
        """
        return parse_music_video(self.provider, await self.api.get(f"videos/{prov_video_id}"))

    async def artist_videos(self, prov_artist_id: str) -> list[MusicVideo]:
        """
        Return the music videos of a Tidal artist, at most ARTIST_MUSIC_VIDEOS_LIMIT.

        :param prov_artist_id: The Tidal id of the artist.
        """
        data = await self.api.get(
            f"artists/{prov_artist_id}/videos", params={"limit": ARTIST_MUSIC_VIDEOS_LIMIT}
        )
        return self._parse_items(data.get("items") or [])

    async def library(self) -> AsyncGenerator[MusicVideo]:
        """Yield the favorite music videos of the user."""
        offset = 0
        while True:
            data = await self.api.get(
                f"users/{self.auth.user_id}/favorites/videos",
                params={"limit": _PAGE_SIZE, "offset": offset, "order": "DATE"},
            )
            items = data.get("items") or []
            for item in items:
                try:
                    yield parse_music_video(self.provider, item)
                except SKIPPABLE_ITEM_ERRORS as err:
                    video_id = (item.get("item") or {}).get("id")
                    self.provider.report_skipped_sync_item(
                        MediaType.MUSIC_VIDEO, str(video_id) if video_id else None, err
                    )
            offset += len(items)
            if not items or offset >= int(data.get("totalNumberOfItems") or 0):
                return

    async def add_favorite(self, prov_video_id: str) -> bool:
        """
        Add a music video to the favorites of the user.

        :param prov_video_id: The Tidal id of the video.
        """
        await self.api.post(
            f"users/{self.auth.user_id}/favorites/videos",
            data={"videoIds": prov_video_id},
            as_form=True,
        )
        return True

    async def remove_favorite(self, prov_video_id: str) -> bool:
        """
        Remove a music video from the favorites of the user.

        :param prov_video_id: The Tidal id of the video.
        """
        await self.api.delete(f"users/{self.auth.user_id}/favorites/videos/{prov_video_id}")
        return True

    async def search(self, search_query: str, limit: int) -> list[MusicVideo]:
        """
        Search the music videos of Tidal.

        :param search_query: The text to search for.
        :param limit: The maximum number of music videos to return.
        """
        data = await self.api.get(
            "search", params={"query": search_query, "types": "VIDEOS", "limit": limit}
        )
        return self._parse_items((data.get("videos") or {}).get("items") or [])[:limit]

    async def get_stream_details(self, prov_video_id: str) -> StreamDetails:
        """
        Return the stream of a music video: its HLS playlist, whose audio the players get.

        :param prov_video_id: The Tidal id of the video.
        """
        video = await self.provider.get_music_video(prov_video_id)
        playback_info = await self.api.get(
            f"videos/{prov_video_id}/playbackinfopostpaywall",
            params={
                "videoquality": _VIDEO_QUALITY,
                "playbackmode": "STREAM",
                "assetpresentation": "FULL",
            },
        )
        return StreamDetails(
            item_id=prov_video_id,
            provider=self.provider.instance_id,
            audio_format=AudioFormat(content_type=ContentType.AAC),
            media_type=MediaType.MUSIC_VIDEO,
            stream_type=StreamType.HLS,
            duration=video.duration,
            path=hls_url_from_playback_info(playback_info),
            can_seek=True,
            allow_seek=True,
        )

    def _parse_items(self, items: list[dict[str, Any]]) -> list[MusicVideo]:
        """Parse a list of Tidal video objects, skipping the ones that do not parse."""
        videos: list[MusicVideo] = []
        for item in items:
            try:
                videos.append(parse_music_video(self.provider, item))
            except SKIPPABLE_ITEM_ERRORS as err:
                self.provider.logger.warning("Skipping Tidal video: %s", err)
        return videos
