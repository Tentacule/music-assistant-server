"""Manage MediaItems of type MusicVideo (fork)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType, MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    ItemMappingSummary,
    MusicVideo,
    MusicVideoSummary,
)

from music_assistant.controllers.music.custom_migrations import (
    DB_TABLE_MUSIC_VIDEO_ARTISTS,
    DB_TABLE_MUSIC_VIDEO_TRACKS,
    DB_TABLE_MUSIC_VIDEOS,
)
from music_assistant.helpers.compare import compare_artists, compare_strings, compare_version
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase
from .music_video_playback import MusicVideoPlayback, MusicVideoPlaybackInfo

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.event import MassEvent

    from music_assistant import MusicAssistant


class MusicVideoTrackLink(StrEnum):
    """How a music video was linked to a library track."""

    ISRC = "isrc"
    MB_RECORDING = "mb_recording"
    NAME = "name"
    MANUAL = "manual"
    # a link the user removed, kept so the automatic matching does not add it back
    EXCLUDED = "excluded"


# the links a new automatic match may not replace
_USER_LINKS: tuple[str, ...] = (MusicVideoTrackLink.MANUAL, MusicVideoTrackLink.EXCLUDED)


class MusicVideosController(MediaControllerBase[MusicVideo]):
    """Controller managing MediaItems of type MusicVideo."""

    db_table = DB_TABLE_MUSIC_VIDEOS
    media_type = MediaType.MUSIC_VIDEO
    item_cls = MusicVideo
    summary_item_cls = MusicVideoSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/by_artist", self.by_artist, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/for_track", self.for_track, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/set_track_link",
            self.set_track_link,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            f"music/{api_base}/remove_track_link",
            self.remove_track_link,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            f"music/{api_base}/video_url", self.video_url, required_scope=Scope.LIBRARY_READ
        )
        self.playback = MusicVideoPlayback(mass)
        # links to a removed artist or track would point at nothing
        self.mass.subscribe(self._on_media_item_deleted, EventType.MEDIA_ITEM_DELETED)

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """Return the base SELECT query for music videos and its bound query params."""
        query = f"""
        SELECT
            {self.db_table}.*,
            {self._external_ids_query()} AS external_ids,
            {self._favorite_query()} AS favorite,
            {self._provider_mappings_query()} AS provider_mappings,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                    'item_id', artists.item_id,
                    'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist',
                    'external_ids', json({self._external_ids_query(MediaType.ARTIST, "artists")})
                )) FROM artists
                JOIN {DB_TABLE_MUSIC_VIDEO_ARTISTS}
                    ON {DB_TABLE_MUSIC_VIDEO_ARTISTS}.artist_id = artists.item_id
                WHERE {DB_TABLE_MUSIC_VIDEO_ARTISTS}.music_video_id = {self.db_table}.item_id
            ) AS artists,
            {self._linked_track_query(full=True)} AS video_track
            FROM {self.db_table}
            """
        return query, {}

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for music video summary listings."""
        query = f"""
        SELECT
            {self._summary_base_columns()},
            {self.db_table}.version,
            {self.db_table}.duration,
            {self._provider_mappings_query()} AS provider_mappings,
            {self._artist_mappings_summary_query(DB_TABLE_MUSIC_VIDEO_ARTISTS, "music_video_id")}
                AS artists,
            {self._linked_track_query(full=False)} AS video_track
            FROM {self.db_table}
            """
        return query, {}

    async def by_artist(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> list[MusicVideo]:
        """
        Return the music videos of an artist.

        :param item_id: The artist id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        :param in_library_only: Only return the music videos in the library, without asking
            the music providers for the other music videos of the artist.
        """
        artist = await self.mass.music.artists.get(item_id, provider_instance_id_or_domain)
        result: list[MusicVideo] = []
        if artist.provider == "library":
            result.extend(
                await self.get_library_items_by_query(
                    extra_query_parts=[
                        f"{self.db_table}.item_id IN (SELECT music_video_id "
                        f"FROM {DB_TABLE_MUSIC_VIDEO_ARTISTS} WHERE artist_id = :artist_id)"
                    ],
                    extra_query_params={"artist_id": int(artist.item_id)},
                    limit=0,
                )
            )
        if in_library_only:
            return result
        known = {(m.provider_instance, m.item_id) for v in result for m in v.provider_mappings}
        for mapping in artist.provider_mappings:
            provider = self.mass.get_provider(mapping.provider_instance)
            if not isinstance(provider, MusicProvider):
                continue
            if ProviderFeature.ARTIST_MUSIC_VIDEOS not in provider.supported_features:
                continue
            for video in await provider.get_artist_music_videos(mapping.item_id):
                if (video.provider, video.item_id) in known:
                    continue
                known.add((video.provider, video.item_id))
                result.append(video)
        return result

    async def for_track(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> list[MusicVideo]:
        """
        Return the library music videos linked to a track.

        :param item_id: The track id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        """
        track = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if track is None:
            return []
        return await self.get_library_items_by_query(
            extra_query_parts=[
                f"{self.db_table}.item_id IN (SELECT music_video_id "
                f"FROM {DB_TABLE_MUSIC_VIDEO_TRACKS} "
                "WHERE track_id = :track_id AND match_source != :excluded)"
            ],
            extra_query_params={
                "track_id": int(track.item_id),
                "excluded": MusicVideoTrackLink.EXCLUDED.value,
            },
            limit=0,
        )

    async def video_url(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> MusicVideoPlaybackInfo:
        """
        Return where a client reads the video of a music video, to show it muted.

        :param item_id: The music video id on the provider, or its library id.
        :param provider_instance_id_or_domain: The provider the id belongs to, or "library".
        """
        music_video = await self.get(item_id, provider_instance_id_or_domain)
        return await self.playback.video_url(music_video)

    async def set_track_link(self, item_id: str, track_id: str) -> MusicVideo:
        """
        Link a library music video to a library track by hand.

        The manual link replaces the automatic links of the music video, which were wrong
        if the user had to correct them.

        :param item_id: The library id of the music video.
        :param track_id: The library id of the track.
        """
        video = await self.get_library_item(item_id)
        track = await self.mass.music.tracks.get_library_item(track_id)
        db_id = int(video.item_id)
        await self.mass.music.database.execute_write(
            f"DELETE FROM {DB_TABLE_MUSIC_VIDEO_TRACKS} "
            "WHERE music_video_id = :video_id AND match_source NOT IN (:manual, :excluded)",
            {
                "video_id": db_id,
                "manual": MusicVideoTrackLink.MANUAL.value,
                "excluded": MusicVideoTrackLink.EXCLUDED.value,
            },
        )
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_MUSIC_VIDEO_TRACKS,
            {
                "music_video_id": db_id,
                "track_id": int(track.item_id),
                "match_source": MusicVideoTrackLink.MANUAL.value,
            },
        )
        return await self._signal_updated(db_id)

    async def remove_track_link(self, item_id: str, track_id: str) -> MusicVideo:
        """
        Remove the link between a library music video and a library track.

        The link stays recorded as excluded, so the automatic matching does not add it back.

        :param item_id: The library id of the music video.
        :param track_id: The library id of the track.
        """
        video = await self.get_library_item(item_id)
        db_id = int(video.item_id)
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_MUSIC_VIDEO_TRACKS,
            {
                "music_video_id": db_id,
                "track_id": int(track_id),
                "match_source": MusicVideoTrackLink.EXCLUDED.value,
            },
        )
        return await self._signal_updated(db_id)

    async def link_track(
        self, item_id: str | int, track_id: str | int, source: MusicVideoTrackLink
    ) -> bool:
        """
        Store an automatic link between a library music video and a library track.

        A link the user made or removed for this pair is kept as it is.

        :param item_id: The library id of the music video.
        :param track_id: The library id of the track.
        :param source: How the link was found.
        :return: Whether the link was stored.
        """
        existing = await self.mass.music.database.get_row(
            DB_TABLE_MUSIC_VIDEO_TRACKS,
            {"music_video_id": int(item_id), "track_id": int(track_id)},
        )
        if existing and existing["match_source"] in _USER_LINKS:
            return False
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_MUSIC_VIDEO_TRACKS,
            {"music_video_id": int(item_id), "track_id": int(track_id), "match_source": source},
        )
        return True

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete a music video from the library, with its artist and track links."""
        db_id = int(item_id)
        await super().remove_item_from_library(db_id, recursive)
        for table in (DB_TABLE_MUSIC_VIDEO_ARTISTS, DB_TABLE_MUSIC_VIDEO_TRACKS):
            await self.mass.music.database.delete(table, {"music_video_id": db_id})

    async def match_providers(self, db_item: MusicVideo) -> None:
        """
        Try to find the music video on the other (streaming) providers.

        A music video is only known on the provider it comes from: two providers rarely
        hold the same video, so they are not matched by name.
        """
        return

    async def _confirm_library_candidate(
        self, db_item: MusicVideo, item: MusicVideo | ItemMapping
    ) -> bool:
        """
        Return True if a library music video that matched on an external id or name is this one.

        :param db_item: Existing library music video that matched.
        :param item: The (provider) music video that is being added to the library.
        """
        if not compare_strings(db_item.name, item.name, strict=True):
            return False
        if not compare_version(db_item.version, item.version):
            return False
        if not isinstance(item, MusicVideo):
            return True
        return compare_artists(list(db_item.artists), list(item.artists), any_match=True)

    async def _add_library_item(self, item: MusicVideo, overwrite_existing: bool = False) -> int:
        """Add a new music video record to the database."""
        if not isinstance(item, MusicVideo):  # an ItemMapping can not be added
            msg = "Not a valid MusicVideo object"  # type: ignore[unreachable]
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Music video is missing artist(s)"
            raise InvalidDataError(msg)
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "duration": item.duration,
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
        await self.set_external_ids(db_id, item.external_ids)
        await self.set_provider_mappings(db_id, item.provider_mappings)
        await self._set_music_video_artists(db_id, item.artists)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: MusicVideo, overwrite: bool = False
    ) -> None:
        """Update an existing music video record in the database."""
        db_id = int(item_id)
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "duration": update.duration if overwrite else cur_item.duration or update.duration,
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        await self._set_music_video_artists(db_id, update.artists, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _set_music_video_artists(
        self, db_id: int, artists: Iterable[Artist | ItemMapping], overwrite: bool = False
    ) -> None:
        """Store the artists of a music video; an empty set never clears the stored rows."""
        await self._set_main_artists(
            DB_TABLE_MUSIC_VIDEO_ARTISTS, "music_video_id", db_id, artists, overwrite
        )

    def _linked_track_query(self, full: bool) -> str:
        """
        Return a subquery selecting the library track linked to the music video, as JSON.

        A manual link wins over the automatic ones; an excluded link is no link.

        :param full: Select the fields of a full ItemMapping instead of a summary one.
        """
        extra_fields = (
            """,
                'provider', 'library',
                'version', COALESCE(tracks.version, ''),
                'media_type', 'track'"""
            if full
            else ""
        )
        return f"""(SELECT json_object(
                'item_id', tracks.item_id,
                'name', tracks.name,
                'sort_name', tracks.sort_name{extra_fields}
            ) FROM {DB_TABLE_MUSIC_VIDEO_TRACKS}
            JOIN tracks ON tracks.item_id = {DB_TABLE_MUSIC_VIDEO_TRACKS}.track_id
            WHERE {DB_TABLE_MUSIC_VIDEO_TRACKS}.music_video_id = {self.db_table}.item_id
                AND {DB_TABLE_MUSIC_VIDEO_TRACKS}.match_source != 'excluded'
            ORDER BY ({DB_TABLE_MUSIC_VIDEO_TRACKS}.match_source = 'manual') DESC, tracks.item_id
            LIMIT 1)"""

    def _parse_summary_row(
        self, db_row: Mapping[str, Any], hidden_sources: set[str]
    ) -> MusicVideoSummary:
        """Parse a raw summary db row into a MusicVideoSummary object."""
        item = cast("MusicVideoSummary", super()._parse_summary_row(db_row, hidden_sources))
        item.version = db_row["version"] or ""
        item.duration = db_row["duration"] or 0
        item.artists = self._parse_summary_artist_mappings(db_row)
        if raw_track := db_row["video_track"]:
            track: dict[str, Any] = json_loads(raw_track)
            item.track = ItemMappingSummary(
                media_type=MediaType.TRACK,
                item_id=str(track["item_id"]),
                provider="library",
                name=track["name"],
                sort_name=track["sort_name"],
            )
        return item

    async def _signal_updated(self, db_id: int) -> MusicVideo:
        """Send the update event of a music video whose track link changed, and return it."""
        video = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, video.uri, video)
        return video

    async def _on_media_item_deleted(self, event: MassEvent) -> None:
        """Remove the links to a library artist or track that was deleted."""
        item = event.data
        if getattr(item, "provider", None) != "library":
            return
        media_type = getattr(item, "media_type", None)
        if media_type == MediaType.ARTIST:
            table, column = DB_TABLE_MUSIC_VIDEO_ARTISTS, "artist_id"
        elif media_type == MediaType.TRACK:
            table, column = DB_TABLE_MUSIC_VIDEO_TRACKS, "track_id"
        else:
            return
        await self.mass.music.database.delete(table, {column: int(item.item_id)})
