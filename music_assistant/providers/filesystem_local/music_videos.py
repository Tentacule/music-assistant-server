"""Music videos of the local filesystem provider (fork)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MusicVideo,
    ProviderMapping,
    UniqueList,
    VideoMetadata,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.helpers.nfo import parse_nfo_root
from music_assistant.helpers.tags import clean_mbid, split_items

from .constants import CONF_CONTENT_TYPE, IMAGE_EXTENSIONS
from .helpers import FileSystemItem

if TYPE_CHECKING:
    from music_assistant.helpers.tags import AudioTags

    from . import LocalFileSystemProvider

CONF_MUSIC_VIDEOS_FOLDER = "music_videos_folder"
CONF_ENTRY_MUSIC_VIDEOS_FOLDER = ConfigEntry(
    key=CONF_MUSIC_VIDEOS_FOLDER,
    type=ConfigEntryType.STRING,
    default_value="",
    required=False,
    depends_on=CONF_CONTENT_TYPE,
    depends_on_value="music",
)

MUSIC_VIDEO_EXTENSIONS = frozenset({"mp4", "m4v", "mkv", "webm", "mov"})
# the image stems next to a video that hold its thumbnail, by preference
_THUMB_SUFFIXES = ("-thumb", "-poster", "-fanart", "")
# the video codecs and containers a browser plays, possibly after a remux to MP4
_WEB_VIDEO_CODECS = frozenset({"h264", "vp8", "vp9", "av1"})
_NFO_ROOT = "musicvideo"
# separator of "Artist - Title" in the name of a video file without NFO
_FILENAME_SEPARATOR = " - "


@dataclass
class MusicVideoNfo:
    """The fields of a Kodi <musicvideo> NFO file that describe a music video."""

    title: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    year: int | None = None
    genres: set[str] = field(default_factory=set)
    plot: str | None = None
    mb_artist_ids: list[str] = field(default_factory=list)
    mb_recording_id: str | None = None
    isrc: str | None = None


def music_videos_folder(value: Any) -> str:
    """
    Return the configured music videos folder as a relative path, or "" when there is none.

    :param value: The raw config value.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().strip("/\\").replace("\\", "/")


def in_music_videos_folder(relative_path: str, folder: str) -> bool:
    """
    Return whether a file or folder lies in the music videos folder.

    :param relative_path: The path relative to the provider root.
    :param folder: The music videos folder, as returned by music_videos_folder.
    """
    if not folder:
        return False
    path = relative_path.replace("\\", "/").strip("/")
    return path == folder or path.startswith(f"{folder}/")


def parse_music_video_nfo(data: bytes) -> MusicVideoNfo | None:
    """
    Parse the bytes of a Kodi <musicvideo> NFO file, or return None when it is not one.

    :param data: The raw NFO file content.
    """
    if (root := parse_nfo_root(data, _NFO_ROOT)) is None:
        return None
    year_text = _text(root.get("year")) or (_text(root.get("premiered")) or "")[:4]
    return MusicVideoNfo(
        title=_text(root.get("title")),
        artists=[name for value in _texts(root.get("artist")) for name in split_items(value)],
        album=_text(root.get("album")),
        year=int(year_text) if year_text and year_text.isdigit() else None,
        genres={name for value in _texts(root.get("genre")) for name in split_items(value)},
        plot=_text(root.get("plot")),
        mb_artist_ids=[
            mbid
            for value in _texts(root.get("musicbrainzartistid"))
            for part in split_items(value)
            if (mbid := clean_mbid(part, "music video nfo"))
        ],
        mb_recording_id=clean_mbid(
            _text(root.get("musicbrainztrackid")) or _text(root.get("musicbrainzrecordingid")),
            "music video nfo",
        ),
        isrc=_text(root.get("isrc")),
    )


def names_from_filename(stem: str) -> tuple[str | None, str]:
    """
    Return the artist and the title a video file name gives, as "Artist - Title".

    :param stem: The file name without its extension.
    """
    artist, separator, title = stem.partition(_FILENAME_SEPARATOR)
    if not separator or not artist.strip() or not title.strip():
        return None, stem.strip()
    return artist.strip(), title.strip()


def video_metadata_from_probe(raw: dict[str, Any]) -> VideoMetadata | None:
    """
    Return the video metadata of a file from its ffprobe output, or None without video.

    :param raw: The raw ffprobe output (streams and format).
    """
    video_stream = next(
        (
            stream
            for stream in raw.get("streams", [])
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video_stream is None:
        return None
    codec = str(video_stream.get("codec_name") or "") or None
    return VideoMetadata(
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        video_codec=codec,
        container=str(raw.get("format", {}).get("format_name") or "") or None,
        web_playable=codec in _WEB_VIDEO_CODECS,
    )


class FilesystemMusicVideos:
    """Reads the music videos in the music videos folder of a filesystem provider."""

    def __init__(self, provider: LocalFileSystemProvider) -> None:
        """
        Initialize the music video reader.

        :param provider: The filesystem provider the music videos belong to.
        """
        self.provider = provider

    @property
    def folder(self) -> str:
        """Return the music videos folder relative to the provider root, "" when unset."""
        if self.provider.media_content_type != "music":
            return ""
        return music_videos_folder(self.provider.config.get_value(CONF_MUSIC_VIDEOS_FOLDER))

    @property
    def enabled(self) -> bool:
        """Return whether the provider has a music videos folder."""
        return bool(self.folder)

    def owns(self, relative_path: str) -> bool:
        """
        Return whether a path lies in the music videos folder, so the audio scan skips it.

        :param relative_path: The path relative to the provider root.
        """
        return in_music_videos_folder(relative_path, self.folder)

    def is_music_video_file(self, relative_path: str) -> bool:
        """
        Return whether a path is a video file of the music videos folder.

        :param relative_path: The path relative to the provider root.
        """
        return self.owns(relative_path) and _is_video_path(relative_path)

    async def iter_library(self) -> AsyncGenerator[MusicVideo]:
        """Yield the music videos of the folder, parsing only the files that changed."""
        stored_checksums = await self._stored_checksums()
        for file_item in await self._list_video_files(self.folder):
            try:
                if stored_checksums.get(file_item.relative_path) == file_item.checksum and (
                    library_item := await self._library_item(file_item.relative_path)
                ):
                    yield library_item
                    continue
                yield await self.parse(file_item)
            except Exception as err:  # noqa: BLE001 - one bad file must not stop the sync
                self.provider.report_skipped_sync_item(
                    MediaType.MUSIC_VIDEO, file_item.relative_path, err
                )

    async def get(self, prov_music_video_id: str) -> MusicVideo:
        """
        Return the music video of a file in the music videos folder.

        :param prov_music_video_id: The path of the video file, relative to the provider root.
        :raises MediaNotFoundError: When the path is not a video file of the folder.
        """
        if not self.is_music_video_file(prov_music_video_id):
            raise MediaNotFoundError(f"Music video not found: {prov_music_video_id}")
        return await self.parse(await self.provider.resolve(prov_music_video_id))

    async def get_stream_details(self, prov_music_video_id: str) -> StreamDetails:
        """
        Return the stream details of a music video, whose audio the players get.

        :param prov_music_video_id: The path of the video file, relative to the provider root.
        """
        video = await self._library_item(prov_music_video_id) or await self.get(prov_music_video_id)
        mapping = next(
            (m for m in video.provider_mappings if m.item_id == prov_music_video_id),
            None,
        )
        file_item = await self.provider.resolve(prov_music_video_id)
        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=prov_music_video_id,
            audio_format=mapping.audio_format if mapping else AudioFormat(),
            media_type=MediaType.MUSIC_VIDEO,
            stream_type=StreamType.LOCAL_FILE,
            duration=video.duration,
            size=file_item.file_size,
            data=file_item,
            path=file_item.absolute_path,
            can_seek=True,
            allow_seek=True,
        )

    async def parse(self, file_item: FileSystemItem) -> MusicVideo:
        """
        Build the music video of a video file, from its NFO, its tags and its name.

        :param file_item: The video file.
        """
        provider = self.provider
        tags = await provider._parse_tags(file_item)
        nfo = await self._read_nfo(file_item) or MusicVideoNfo()
        file_artist, file_title = names_from_filename(file_item.name)
        title = nfo.title or tags.tags.get("title") or file_title
        artist_names = nfo.artists or _tag_artists(tags) or ([file_artist] if file_artist else [])
        if not artist_names:
            msg = f"No artist for music video {file_item.relative_path}"
            raise MediaNotFoundError(msg)
        artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for index, name in enumerate(artist_names):
            mbid = nfo.mb_artist_ids[index] if index < len(nfo.mb_artist_ids) else None
            artists.append(await provider._parse_artist(name, mbid=mbid))
        video = MusicVideo(
            item_id=file_item.relative_path,
            provider=provider.instance_id,
            name=str(title),
            duration=int(tags.duration or 0),
            artists=artists,
            video_metadata=video_metadata_from_probe(tags.raw),
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=provider.domain,
                    provider_instance=provider.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        codec_type=provider._codec_type_from_tags(tags),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                    in_library=True,
                )
            },
        )
        if nfo.album:
            video.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=nfo.album,
                provider=provider.instance_id,
                name=nfo.album,
            )
        if nfo.year:
            video.metadata.release_date = datetime(nfo.year, 1, 1, tzinfo=UTC)
        if nfo.genres:
            video.metadata.genres = nfo.genres
        if nfo.plot:
            video.metadata.description = nfo.plot
        if isrc := nfo.isrc or tags.tags.get("isrc"):
            video.external_ids.add((ExternalID.ISRC, str(isrc)))
        if nfo.mb_recording_id:
            video.external_ids.add((ExternalID.MB_RECORDING, nfo.mb_recording_id))
        if thumb := await self._thumb_image(file_item):
            video.metadata.add_image(thumb)
        return video

    async def _list_video_files(self, folder: str) -> list[FileSystemItem]:
        """Return the video files of a folder and its subfolders."""
        files: list[FileSystemItem] = []
        for item in await self.provider._scandir(folder, use_cache=False):
            if item.is_dir:
                files.extend(await self._list_video_files(item.relative_path))
            elif _is_video_path(item.relative_path):
                files.append(item)
            await asyncio.sleep(0)
        return files

    async def _read_nfo(self, file_item: FileSystemItem) -> MusicVideoNfo | None:
        """Return the NFO of a video file (same name, .nfo extension), if it has one."""
        nfo_path = f"{file_item.relative_path.rsplit('.', 1)[0]}.nfo"
        if not await self.provider.exists(nfo_path):
            return None
        data = await self.provider._read_file(nfo_path)
        return await asyncio.to_thread(parse_music_video_nfo, data)

    async def _thumb_image(self, file_item: FileSystemItem) -> MediaItemImage | None:
        """Return the thumbnail image next to a video file, if there is one."""
        stem = file_item.relative_path.rsplit(".", 1)[0]
        folder = stem.rsplit("/", 1)[0] if "/" in stem else ""
        siblings = {
            item.relative_path.lower(): item.relative_path
            for item in await self.provider._scandir(folder)
            if not item.is_dir and (item.ext or "").lower() in IMAGE_EXTENSIONS
        }
        for suffix in _THUMB_SUFFIXES:
            for ext in sorted(IMAGE_EXTENSIONS):
                if path := siblings.get(f"{stem}{suffix}.{ext}".lower()):
                    return MediaItemImage(
                        type=ImageType.THUMB,
                        path=path,
                        provider=self.provider.instance_id,
                        remotely_accessible=False,
                    )
        return None

    async def _stored_checksums(self) -> dict[str, str]:
        """Return the checksum stored for each music video file of this provider."""
        rows = await self.provider.mass.music.database.get_rows_from_query(
            f"SELECT provider_item_id, details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            "WHERE provider_instance = :instance AND media_type = :media_type",
            {"instance": self.provider.instance_id, "media_type": MediaType.MUSIC_VIDEO.value},
            limit=0,
        )
        return {row["provider_item_id"]: str(row["details"]) for row in rows}

    async def _library_item(self, prov_music_video_id: str) -> MusicVideo | None:
        """Return the library music video of a file, if it is in the library."""
        return await self.provider.mass.music.music_videos.get_library_item_by_prov_id(
            prov_music_video_id, self.provider.instance_id
        )


def _is_video_path(path: str) -> bool:
    """Return whether a path names a video file by its extension."""
    return "." in path and path.rsplit(".", 1)[1].lower() in MUSIC_VIDEO_EXTENSIONS


def _tag_artists(tags: AudioTags) -> list[str]:
    """Return the artist names in the container tags of a video file."""
    if not (value := tags.tags.get("artist")):
        return []
    return list(split_items(str(value)))


def _texts(value: Any) -> list[str]:
    """Return the text values of an NFO element that may be repeated."""
    values = value if isinstance(value, list) else [value]
    return [text for item in values if (text := _text(item))]


def _text(value: Any) -> str | None:
    """Return the stripped text of an NFO element, or None when it is empty or not text."""
    if isinstance(value, dict):
        value = value.get("#text")
    if not isinstance(value, str):
        return None
    return value.strip() or None
