"""The video of a music video, served to the clients that show it (fork)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from aiohttp import web
from mashumaro import DataClassDictMixin
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnsupportedFeaturedException

from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import MusicVideo

    from music_assistant import MusicAssistant

MUSIC_VIDEO_ROUTE_PREFIX: Final[str] = "/music_video/"
# how long a video URL handed to a client keeps working
VIDEO_URL_LIFETIME: Final[int] = 6 * 3600
# the containers a browser and ExoPlayer read as they are, with HTTP range requests for seeking
_DIRECT_CONTAINERS: Final[frozenset[str]] = frozenset({"mp4", "m4v", "mov", "webm"})
_MIME_TYPES: Final[dict[str, str]] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
}


class MusicVideoSourceKind(StrEnum):
    """How a client reads the video of a music video."""

    # a file served as it is, which the client seeks with HTTP range requests
    FILE = "file"
    # a file remuxed to fragmented MP4 on the fly; to seek, the client asks again with ?start=
    REMUX = "remux"
    # an HLS playlist of the music service, which the client reads itself
    HLS = "hls"


@dataclass
class MusicVideoPlaybackInfo(DataClassDictMixin):
    """Where and how a client reads the video of a music video."""

    url: str
    kind: MusicVideoSourceKind
    # whether the client can seek in the video by itself
    seekable: bool
    # unix time after which the url may no longer work, so the client asks for a new one
    expires_at: int
    # False when the video codec is known not to play in a browser
    web_playable: bool = True


@dataclass
class _ServedFile:
    """A local video file a token gives access to."""

    path: str
    remux: bool
    expires_at: float


class MusicVideoPlayback:
    """Gives the clients the video of a music video, the players getting its audio."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the music video playback.

        :param mass: The Music Assistant instance.
        """
        self.mass = mass
        self._served: dict[str, _ServedFile] = {}
        self._route_registered = False

    async def video_url(self, music_video: MusicVideo) -> MusicVideoPlaybackInfo:
        """
        Return where a client reads the video of a music video.

        :param music_video: The music video, from the library or a provider.
        :raises UnsupportedFeaturedException: When the source of the video can not be shown.
        """
        provider_instance, item_id = self._source_of(music_video)
        provider = self.mass.get_provider(provider_instance)
        if not isinstance(provider, MusicProvider):
            raise MediaNotFoundError(f"Provider {provider_instance} is not available")
        streamdetails = await provider.get_stream_details(item_id, MediaType.MUSIC_VIDEO)
        web_playable = (
            music_video.video_metadata.web_playable if music_video.video_metadata else True
        )
        if streamdetails.stream_type == StreamType.LOCAL_FILE and streamdetails.path:
            return self._serve_local_file(streamdetails.path, web_playable)
        if streamdetails.path and streamdetails.path.startswith("http"):
            # fork: the client reads the HLS playlist of the music service itself
            return MusicVideoPlaybackInfo(
                url=streamdetails.path,
                kind=MusicVideoSourceKind.HLS,
                seekable=True,
                expires_at=int(time.time()) + streamdetails.expiration,
                web_playable=web_playable,
            )
        raise UnsupportedFeaturedException(f"The video of {music_video.name} can not be shown")

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """
        Serve a local video file for a `/music_video/<token>?start=` request.

        :param request: The request of the client.
        """
        token = request.path[len(MUSIC_VIDEO_ROUTE_PREFIX) :].strip("/")
        served = self._served.get(token)
        if served is None or served.expires_at < time.time():
            raise web.HTTPNotFound(reason="Unknown or expired music video")
        if not served.remux:
            ext = served.path.rsplit(".", 1)[-1].lower()
            return web.FileResponse(
                served.path, headers={"Content-Type": _MIME_TYPES.get(ext, "video/mp4")}
            )
        try:
            start = max(0.0, float(request.query.get("start", "0")))
        except ValueError as err:
            raise web.HTTPBadRequest(reason="Invalid start position") from err
        return await self._serve_remux(request, served.path, start)

    def _serve_local_file(self, path: str, web_playable: bool) -> MusicVideoPlaybackInfo:
        """Give access to a local video file through a token, and return its URL."""
        self._ensure_route()
        now = time.time()
        for token in [t for t, served in self._served.items() if served.expires_at < now]:
            del self._served[token]
        remux = path.rsplit(".", 1)[-1].lower() not in _DIRECT_CONTAINERS
        token = secrets.token_urlsafe(32)
        expires_at = now + VIDEO_URL_LIFETIME
        self._served[token] = _ServedFile(path=path, remux=remux, expires_at=expires_at)
        return MusicVideoPlaybackInfo(
            url=f"{self.mass.webserver.base_url}{MUSIC_VIDEO_ROUTE_PREFIX}{token}",
            kind=MusicVideoSourceKind.REMUX if remux else MusicVideoSourceKind.FILE,
            seekable=not remux,
            expires_at=int(expires_at),
            web_playable=web_playable,
        )

    async def _serve_remux(
        self, request: web.Request, path: str, start: float
    ) -> web.StreamResponse:
        """Stream a video file remuxed to fragmented MP4, from a position, without its audio."""
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            *(["-ss", f"{start:.3f}"] if start else []),
            "-i",
            path,
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-",
        ]
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        response.enable_chunked_encoding()
        await response.prepare(request)
        async with AsyncProcess(args, stdout=True) as ffmpeg_proc:
            async for chunk in ffmpeg_proc.iter_chunked():
                await response.write(chunk)
        await response.write_eof()
        return response

    def _ensure_route(self) -> None:
        """Register the route of the local video files on the webserver, once."""
        if self._route_registered:
            return
        self.mass.webserver.register_dynamic_route(
            f"{MUSIC_VIDEO_ROUTE_PREFIX}*", self.handle_request, "GET"
        )
        self._route_registered = True

    def _source_of(self, music_video: MusicVideo) -> tuple[str, str]:
        """Return the provider instance and item id that serve the video of a music video."""
        if music_video.provider != "library":
            return music_video.provider, music_video.item_id
        for mapping in sorted(music_video.provider_mappings, key=lambda m: not m.available):
            if self.mass.get_provider(mapping.provider_instance):
                return mapping.provider_instance, mapping.item_id
        raise MediaNotFoundError(f"No provider serves the video of {music_video.name}")
