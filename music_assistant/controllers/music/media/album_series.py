"""Edit the album series of the library: their type, the user's own series and their albums."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    AlbumSeriesSource,
    AlbumSeriesType,
    EventType,
    ListingType,
    MediaType,
)
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    MediaCollection,
    MediaItemCollection,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.controllers.music.custom_migrations import (
    DB_TABLE_ALBUM_SERIES_EXCLUSIONS,
    DB_TABLE_ALBUM_SERIES_HIDDEN,
    DB_TABLE_ALBUM_SERIES_INFO,
)
from music_assistant.controllers.music.listing import apply_listing
from music_assistant.helpers.collections import (
    get_collection_item_id,
    get_collection_name_from_item_id,
    parse_collection_sequence,
    remove_item_collections,
    set_item_collection,
)
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.release_groups import write_group_metadata

from .album_groups import (
    _API_BASE,
    _GROUP_KEY_SQL,
    _RELEASE_GROUP_SQL,
    AlbumGroupsMixin,
    _sequence_key,
)

if TYPE_CHECKING:
    from music_assistant_models.enums import SortDirection, SortField

# the API parses no Literal, so the album visibility of a series is a plain string
_ALBUM_VISIBILITIES: Final[tuple[str, ...]] = ("visible", "hidden", "all")

_DEFAULT_INFO: Final[tuple[AlbumSeriesType, AlbumSeriesSource]] = (
    AlbumSeriesType.ALBUM_SERIES,
    AlbumSeriesSource.MUSICBRAINZ,
)


class AlbumSeriesMixin(AlbumGroupsMixin):
    """
    The editing of the album series, on top of the release-group listings.

    A series is the entries with its title in the collections of the library albums. The
    user's own series (custom) also has a row in the series info, so it exists while empty.
    An album removed from a series of MusicBrainz is excluded from it: its entry leaves the
    album and the exclusion keeps a refresh of the metadata from adding it back.
    """

    async def album_series(
        self,
        hidden: bool = False,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        random_seed: int | None = None,
        series_type: AlbumSeriesType | None = None,
    ) -> list[MediaCollection[Album]]:
        """
        Return the album series of the library, each with its albums in series order.

        :param hidden: List the hidden series instead of the shown ones.
        :param search: Only list the series whose name or one of its albums matches.
        :param limit: Maximum number of series to return; 0 returns them all.
        :param offset: Number of series to skip.
        :param sort_field: Sort field to use.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param random_seed: Keep random ordering stable across pages with the same seed.
        :param series_type: Only list the series of this type.
        """
        series = await super().album_series(
            hidden=hidden,
            search=search,
            limit=0,
            sort_field=sort_field,
            sort_direction=sort_direction,
            random_seed=random_seed,
        )
        info = await self._series_info()
        listed = {item.name for item in series}
        hidden_titles = await self._hidden_series_titles()
        safe_search = create_safe_string(search, True, True) if search else None
        # an empty series of the user's own has no album to be listed through
        for title, (_, source) in sorted(info.items(), key=lambda entry: entry[0].casefold()):
            if source != AlbumSeriesSource.CUSTOM or title in listed:
                continue
            if (title in hidden_titles) != hidden:
                continue
            if safe_search and safe_search not in create_safe_string(title, True, True):
                continue
            series.append(_empty_series(title))
        excluded = await self._excluded_album_ids()
        for item in series:
            _describe_series(item, info, excluded.get(item.name, []))
        if series_type:
            series = [item for item in series if item.metadata.series_type == series_type]
        return series[offset : offset + limit] if limit else series[offset:]

    async def get_collection(
        self,
        item_id: str,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        favorite: bool | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        album_visibility: str = "visible",
    ) -> MediaCollection[Album]:
        """
        Return an album series with one album per release group.

        :param item_id: The item id of the series.
        :param search: Only include the albums whose name contains this text.
        :param limit: Maximum number of albums to return; omitted or 0 returns them all.
        :param offset: Number of albums to skip.
        :param favorite: Only include the albums the current user likes (True) or dislikes.
        :param sort_field: Sort field; the series order when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param album_visibility: "visible" (default) lists the albums in the series, "hidden"
            the albums removed from it, "all" both.
        """
        if album_visibility not in _ALBUM_VISIBILITIES:
            msg = f"Unknown album visibility {album_visibility}"
            raise InvalidDataError(msg)
        title = get_collection_name_from_item_id(item_id)
        visible_only = album_visibility == "visible"
        info = await self._series_info()
        hidden_albums = await self._excluded_albums(title)
        try:
            collection = await super().get_collection(
                item_id,
                search=search,
                limit=limit if visible_only else None,
                offset=offset if visible_only else 0,
                favorite=favorite,
                sort_field=sort_field if visible_only else None,
                sort_direction=sort_direction if visible_only else None,
            )
        except MediaNotFoundError:
            if title not in info and not hidden_albums:
                raise
            collection = _empty_series(title)
        _describe_series(collection, info, [album.item_id for _, album in hidden_albums])
        if visible_only:
            return collection
        hidden_albums = _filter_albums(hidden_albums, search, favorite)
        entries = list(hidden_albums)
        if album_visibility == "all":
            entries += [(_series_sequence(album, title), album) for album in collection.items]
        items: list[Album] = [
            album for _, album in sorted(entries, key=lambda entry: _sequence_key(entry[0]))
        ]
        if sort_field or sort_direction:
            items = list(
                apply_listing(
                    items,
                    ListingType.ALBUM_SERIES,
                    sort_field=sort_field,
                    sort_direction=sort_direction,
                )
            )
        if offset or limit:
            items = items[offset : offset + limit] if limit else items[offset:]
        collection.items = UniqueList(items)
        return collection

    async def create_album_series(
        self, name: str, series_type: AlbumSeriesType = AlbumSeriesType.PERSONAL
    ) -> MediaCollection[Album]:
        """
        Create a series of the user's own, empty until albums are added to it.

        :param name: The name of the series.
        :param series_type: The type of the series.
        :raises InvalidDataError: When the name is empty or a series already has it.
        """
        title = name.strip()
        if not title:
            msg = "A series needs a name"
            raise InvalidDataError(msg)
        if await self._series_exists(title):
            msg = f"A series named {title} already exists"
            raise InvalidDataError(msg)
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_SERIES_INFO,
            {
                "title": title,
                "series_type": series_type.value,
                "source": AlbumSeriesSource.CUSTOM.value,
            },
        )
        collection = _empty_series(title)
        _describe_series(collection, await self._series_info(), [])
        return collection

    async def set_album_series_type(self, item_id: str, series_type: AlbumSeriesType) -> None:
        """
        Set the type of a series, which a refresh of the metadata keeps.

        :param item_id: The item id of the series.
        :param series_type: The type of the series.
        """
        title = await self._existing_series_title(item_id)
        _, source = (await self._series_info()).get(title, _DEFAULT_INFO)
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_SERIES_INFO,
            {"title": title, "series_type": series_type.value, "source": source.value},
        )

    async def delete_album_series(self, item_id: str) -> None:
        """
        Delete a series of the user's own, taking its albums out of it.

        A series of MusicBrainz comes back with the next refresh, so it is hidden instead.

        :param item_id: The item id of the series.
        :raises InvalidDataError: When the series is not one of the user's own.
        """
        title = get_collection_name_from_item_id(item_id)
        _, source = (await self._series_info()).get(title, _DEFAULT_INFO)
        if source != AlbumSeriesSource.CUSTOM:
            msg = f"Series {title} comes from MusicBrainz: hide it instead"
            raise InvalidDataError(msg)
        album_ids = await self._series_album_ids(title)
        await self._write_collections(
            {
                album_id: remove_item_collections(collections, (title,))
                for album_id, collections in (await self._stored_collections(album_ids)).items()
            }
        )
        database = self.mass.music.database
        for table in (
            DB_TABLE_ALBUM_SERIES_INFO,
            DB_TABLE_ALBUM_SERIES_HIDDEN,
            DB_TABLE_ALBUM_SERIES_EXCLUSIONS,
        ):
            await database.delete(table, {"title": title})
        await self._signal_albums_updated(album_ids)

    async def add_album_series_album(
        self, item_id: str, album_id: str, sequence: str | None = None
    ) -> None:
        """
        Add a library album to a series, or give it a new number in it.

        Every library version of the album is in the series, as the versions share them.

        :param item_id: The item id of the series.
        :param album_id: The library id of the album.
        :param sequence: The number of the album in the series ("3", "1.5", "IV"), if any.
        """
        title = await self._existing_series_title(item_id)
        await self._put_in_series(title, int(album_id), parse_collection_sequence(sequence))

    async def remove_album_series_album(self, item_id: str, album_id: str) -> None:
        """
        Take a library album out of a series.

        An album leaves a series of the user's own. It is hidden in a series of MusicBrainz,
        so that a refresh of the metadata does not add it back, and it can be restored.

        :param item_id: The item id of the series.
        :param album_id: The library id of the album.
        """
        title = await self._existing_series_title(item_id)
        version_ids = await self._album_version_ids(int(album_id))
        stored = await self._stored_collections(version_ids)
        sequence = next(
            (
                collection.sequence
                for collections in stored.values()
                for collection in collections
                if collection.title == title
            ),
            None,
        )
        _, source = (await self._series_info()).get(title, _DEFAULT_INFO)
        if source != AlbumSeriesSource.CUSTOM:
            for version_id in version_ids:
                await self.mass.music.database.insert_or_replace(
                    DB_TABLE_ALBUM_SERIES_EXCLUSIONS,
                    {"title": title, "album_id": version_id, "sequence": json_dumps(sequence)},
                )
        await self._write_collections(
            {
                version_id: remove_item_collections(collections, (title,))
                for version_id, collections in stored.items()
            }
        )
        await self._signal_albums_updated(version_ids)

    async def restore_album_series_album(self, item_id: str, album_id: str) -> None:
        """
        Put an album removed from a series of MusicBrainz back in it, at its number.

        :param item_id: The item id of the series.
        :param album_id: The library id of the album.
        """
        title = await self._existing_series_title(item_id)
        version_ids = await self._album_version_ids(int(album_id))
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT sequence FROM {DB_TABLE_ALBUM_SERIES_EXCLUSIONS} "
            "WHERE title = :title AND album_id IN :album_ids",
            {"title": title, "album_ids": version_ids},
            limit=1,
        )
        sequence = json_loads(rows[0]["sequence"]) if rows and rows[0]["sequence"] else None
        await self._put_in_series(title, int(album_id), sequence)

    def _register_album_group_commands(self) -> None:
        """Register the release-group API commands and those editing the series."""
        super()._register_album_group_commands()
        for name, handler in (
            ("create_series", self.create_album_series),
            ("set_series_type", self.set_album_series_type),
            ("delete_series", self.delete_album_series),
            ("add_series_album", self.add_album_series_album),
            ("remove_series_album", self.remove_album_series_album),
            ("restore_series_album", self.restore_album_series_album),
        ):
            self.mass.register_api_command(
                f"{_API_BASE}/{name}", handler, required_scope=Scope.LIBRARY_WRITE
            )

    async def _share_release_group_metadata(self, library_id: int) -> None:
        """Keep the albums removed from a series out of it, then share the group's metadata."""
        await self._keep_series_exclusions(library_id)
        await super()._share_release_group_metadata(library_id)

    async def _keep_series_exclusions(self, library_id: int) -> None:
        """
        Take an album that was written again out of the series it was removed from.

        Its tags or MusicBrainz give the series again on each refresh. A version of the album
        is removed from the series its other versions were removed from.

        :param library_id: The library id of the album that was added or updated.
        """
        version_ids = await self._album_version_ids(library_id)
        database = self.mass.music.database
        rows = await database.get_rows_from_query(
            f"SELECT title, album_id, sequence FROM {DB_TABLE_ALBUM_SERIES_EXCLUSIONS} "
            "WHERE album_id IN :album_ids",
            {"album_ids": version_ids},
            limit=0,
        )
        if not rows:
            return
        sequences = {row["title"]: row["sequence"] for row in rows}
        known = {(row["title"], row["album_id"]) for row in rows}
        for title, sequence in sequences.items():
            for version_id in version_ids:
                if (title, version_id) not in known:
                    await database.insert_or_replace(
                        DB_TABLE_ALBUM_SERIES_EXCLUSIONS,
                        {"title": title, "album_id": version_id, "sequence": sequence},
                    )
        changes = {
            version_id: kept
            for version_id, collections in (await self._stored_collections(version_ids)).items()
            if len(kept := remove_item_collections(collections, sequences)) != len(collections)
        }
        await self._write_collections(changes)
        await self._signal_albums_updated([key for key in changes if key != library_id])

    async def _put_in_series(self, title: str, album_id: int, sequence: float | str | None) -> None:
        """Put every library version of an album in a series, at the given number."""
        version_ids = await self._album_version_ids(album_id)
        for version_id in version_ids:
            await self.mass.music.database.delete(
                DB_TABLE_ALBUM_SERIES_EXCLUSIONS, {"title": title, "album_id": version_id}
            )
        await self._write_collections(
            {
                version_id: set_item_collection(collections, title, sequence)
                for version_id, collections in (await self._stored_collections(version_ids)).items()
            }
        )
        await self._signal_albums_updated(version_ids)

    async def _album_version_ids(self, album_id: int) -> list[int]:
        """
        Return the library ids of the versions of a library album, the album included.

        :raises MediaNotFoundError: When the album is not in the library.
        """
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT {DB_TABLE_ALBUMS}.item_id AS item_id FROM {DB_TABLE_ALBUMS} "
            f"WHERE {DB_TABLE_ALBUMS}.item_id = :album_id OR ({_RELEASE_GROUP_SQL} IS NOT NULL "
            f"AND {_RELEASE_GROUP_SQL} = (SELECT {_RELEASE_GROUP_SQL} FROM {DB_TABLE_ALBUMS} "
            f"WHERE {DB_TABLE_ALBUMS}.item_id = :album_id))",
            {"album_id": album_id},
            limit=0,
        )
        version_ids = sorted(int(row["item_id"]) for row in rows)
        if album_id not in version_ids:
            msg = f"Album {album_id} is not in the library"
            raise MediaNotFoundError(msg)
        return version_ids

    async def _stored_collections(
        self, album_ids: list[int]
    ) -> dict[int, list[MediaItemCollection]]:
        """Return the stored collections of library albums, by library id."""
        if not album_ids:
            return {}
        rows = await self.mass.music.database.get_rows_from_query(
            f"""SELECT item_id, json_extract(metadata, '$.collections') AS collections
                FROM {DB_TABLE_ALBUMS} WHERE item_id IN :album_ids""",
            {"album_ids": album_ids},
            limit=0,
        )
        return {
            int(row["item_id"]): [
                MediaItemCollection.from_dict(entry)
                for entry in json_loads(row["collections"] or "[]")
            ]
            for row in rows
        }

    async def _write_collections(
        self, collections_by_album: dict[int, UniqueList[MediaItemCollection]]
    ) -> None:
        """Store the collections of library albums."""
        for album_id, collections in collections_by_album.items():
            await write_group_metadata(
                self.mass.music.database,
                album_id,
                {"collections": [collection.to_dict() for collection in collections]},
            )

    async def _signal_albums_updated(self, album_ids: list[int]) -> None:
        """Tell the clients the albums changed."""
        for album_id in album_ids:
            try:
                album = await self.get_library_item(album_id)
            except MediaNotFoundError:
                continue
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, album.uri, album)

    async def _series_info(self) -> dict[str, tuple[AlbumSeriesType, AlbumSeriesSource]]:
        """Return the type and source of the series that have them, by title."""
        rows = await self.mass.music.database.get_rows(DB_TABLE_ALBUM_SERIES_INFO, limit=0)
        return {
            row["title"]: (AlbumSeriesType(row["series_type"]), AlbumSeriesSource(row["source"]))
            for row in rows
        }

    async def _series_album_ids(self, title: str) -> list[int]:
        """Return the library ids of the albums in a series."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"""SELECT DISTINCT {DB_TABLE_ALBUMS}.item_id AS item_id
                FROM {DB_TABLE_ALBUMS},
                    json_each(json_extract({DB_TABLE_ALBUMS}.metadata, '$.collections')) AS entry
                WHERE json_extract(entry.value, '$.title') = :title""",
            {"title": title},
            limit=0,
        )
        return [int(row["item_id"]) for row in rows]

    async def _series_exists(self, title: str) -> bool:
        """Return whether a series with this title has info, albums or removed albums."""
        if title in await self._series_info() or await self._series_album_ids(title):
            return True
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_ALBUM_SERIES_EXCLUSIONS, {"title": title}, limit=1
        )
        return bool(rows)

    async def _existing_series_title(self, item_id: str) -> str:
        """
        Return the title of a series that exists.

        :raises MediaNotFoundError: When there is no such series.
        """
        title = get_collection_name_from_item_id(item_id)
        if not await self._series_exists(title):
            msg = f"Series {title} not found"
            raise MediaNotFoundError(msg)
        return title

    async def _excluded_album_ids(self) -> dict[str, list[str]]:
        """Return the albums removed from each series, one library id per release group."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"""SELECT exclusions.title AS title, MIN(exclusions.album_id) AS album_id
                FROM {DB_TABLE_ALBUM_SERIES_EXCLUSIONS} exclusions
                JOIN {DB_TABLE_ALBUMS} ON {DB_TABLE_ALBUMS}.item_id = exclusions.album_id
                GROUP BY exclusions.title, {_GROUP_KEY_SQL}""",
            limit=0,
        )
        excluded: dict[str, list[str]] = {}
        for row in rows:
            excluded.setdefault(row["title"], []).append(str(row["album_id"]))
        return excluded

    async def _excluded_albums(self, title: str) -> list[tuple[Any, Album]]:
        """Return the albums removed from a series with their number, one per release group."""
        rows = await self.mass.music.database.get_rows(
            DB_TABLE_ALBUM_SERIES_EXCLUSIONS, {"title": title}, limit=0
        )
        if not rows:
            return []
        sequences = {
            str(row["album_id"]): json_loads(row["sequence"]) if row["sequence"] else None
            for row in rows
        }
        albums = await self.get_library_items_by_query(
            limit=0,
            extra_query_parts=[f"{DB_TABLE_ALBUMS}.item_id IN :series_excluded_ids"],
            extra_query_params={"series_excluded_ids": [int(key) for key in sequences]},
        )
        return [
            (sequences.get(album.item_id), album)
            for album in await self._collapse_groups(list(albums))
        ]


def _empty_series(title: str) -> MediaCollection[Album]:
    """Return a series without albums."""
    return MediaCollection(
        item_id=get_collection_item_id(title, MediaType.ALBUM),
        provider="library",
        name=title,
        provider_mappings=set(),
        items=UniqueList(),
    )


def _describe_series(
    collection: MediaCollection[Album],
    info: dict[str, tuple[AlbumSeriesType, AlbumSeriesSource]],
    hidden_item_ids: list[str],
) -> None:
    """Give a series its type, its source and the albums removed from it."""
    series_type, source = info.get(collection.name, _DEFAULT_INFO)
    collection.metadata.series_type = series_type
    collection.metadata.series_source = source
    collection.metadata.hidden_item_ids = hidden_item_ids
    if collection.metadata.total_items is None:
        collection.metadata.total_items = len(collection.items)


def _series_sequence(album: Album, title: str) -> Any:
    """Return the number of an album in a series."""
    for collection in album.metadata.collections or ():
        if collection.title == title:
            return collection.sequence
    return None


def _filter_albums(
    albums: list[tuple[Any, Album]], search: str | None, favorite: bool | None
) -> list[tuple[Any, Album]]:
    """Keep the albums whose name contains the search and that match the favorite filter."""
    if search:
        safe_search = create_safe_string(search, True, True)
        albums = [
            entry
            for entry in albums
            if safe_search in create_safe_string(entry[1].name, True, True)
        ]
    if favorite is not None:
        albums = [entry for entry in albums if entry[1].favorite is favorite]
    return albums
